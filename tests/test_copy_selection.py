"""Text can be drag-selected over several lines and copied with Ctrl+C.

Each test drives the widget through its window handle, the way a real mouse does: a press, moves with the button held,
and a release, then Ctrl+C through the same window. It then reads the clipboard. Offscreen Qt, fakes only: no socket,
no process, no real settings or keychain, and no modal dialog waits for an answer.
"""

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QGuiApplication, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import claude_processes, local_client
from meridian_desktop.claude_processes import ClaudeProcessesResult
from meridian_desktop.data_source import DataSources
from meridian_desktop.home_status import CHECKING_HEADLINE, CardView, RowView
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.runner_controls import START
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tools_status import YES, ToolRow, ToolsStatus
from meridian_desktop.ui import app as ui_app
from meridian_desktop.ui import client_setup_section, confirm, sessions_panel
from meridian_desktop.ui.first_run import FirstRunOutcome
from meridian_desktop.ui.home_tab import API_NOT_READY_TEXT, LAUNCH_CANCELLED_TEXT, HomeDeps, HomeTab, _Card
from meridian_desktop.ui.main_window import STATUS_MESSAGE, MainWindow
from meridian_desktop.ui.projects_tab import ProjectsTab
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.scope_controls import CANCELLED_TEXT, CORE_NOT_SET_TEXT
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.text import (
    SELECTABLE_TEXT,
    BlockLine,
    block_html,
    paragraph_tops,
    set_label_text,
    text_bottom,
    visible_text,
)
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus

LEFT = Qt.MouseButton.LeftButton
NO_KEYS = Qt.KeyboardModifier.NoModifier
BEFORE = "<<before>>"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"


# ----------------------------------------------------------------- a mouse and a keyboard, through the window handle


def _activate(window: QWidget) -> None:
    window.show()
    window.activateWindow()
    assert QTest.qWaitForWindowActive(window, 3000)  # a shortcut fires only in the active window


def _drag(window: QWidget, start_widget: QWidget, start: QPoint, end_widget: QWidget, end: QPoint, steps: int = 12):
    """Press, move with the button held, and release, through the top-level window. Qt routes it as a real mouse:
    the widget under the press gets the press and click focus, and the moves and the release go to that widget."""
    handle = window.windowHandle()
    begin = start_widget.mapTo(window, start)
    finish = end_widget.mapTo(window, end)
    QTest.mouseMove(handle, begin)
    QTest.mousePress(handle, LEFT, NO_KEYS, begin)
    for step in range(1, steps + 1):
        QTest.mouseMove(
            handle,
            QPoint(begin.x() + (finish.x() - begin.x()) * step // steps, begin.y() + (finish.y() - begin.y()) * step // steps),
        )
    QTest.mouseRelease(handle, LEFT, NO_KEYS, finish)
    QApplication.processEvents()


def _drag_over(window: QWidget, label: QLabel) -> None:
    """Drag from the first character of a label to its last line's end.

    The drag ends just right of the label, on its last line: a label sized to its text (a message box's) ends at its
    last character, so a point inside its right edge can land on the first half of that character and leave it out.
    The label has the mouse from the press, so the end still reaches it.
    """
    rect = label.rect()
    _drag(window, label, QPoint(1, 3), label, QPoint(rect.width() + 4, rect.height() - 2))


def _click(window: QWidget, widget: QWidget, point: QPoint) -> None:
    where = widget.mapTo(window, point)
    QTest.mouseMove(window.windowHandle(), where)
    QTest.mouseClick(window.windowHandle(), LEFT, NO_KEYS, where)
    QApplication.processEvents()


def _ctrl_c(window: QWidget) -> str:
    """Ctrl+C through the window, as a key press reaches the focus widget. Returns what the clipboard holds after."""
    clipboard = QGuiApplication.clipboard()
    clipboard.setText(BEFORE)
    handle = window.windowHandle()
    QTest.keyClick(handle, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    # QTest's own Ctrl release still carries Ctrl, so the application would go on reading Ctrl as held, and a later
    # test's selectRow would add to the selection. A plain release leaves no key held.
    QTest.keyRelease(handle, Qt.Key.Key_Control, NO_KEYS)
    QApplication.processEvents()
    assert QGuiApplication.keyboardModifiers() == NO_KEYS
    return clipboard.text()


def _deferred_deletes() -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ----------------------------------------------------------------- the shared helper


def test_visible_text_reads_the_shown_labels_top_to_bottom_and_skips_hidden_ones_and_buttons(qtbot):
    root = QFrame()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    first = QLabel("First line", root)
    hidden = QLabel("Hidden line", root)
    button = QPushButton("Button text", root)
    second = QLabel("Second line", root)
    empty = QLabel("", root)
    rich = QLabel("<b>Bold</b> words", root)
    rich.setTextFormat(Qt.TextFormat.RichText)
    box = QWidget(root)
    QVBoxLayout(box).addWidget(QLabel("Inside a hidden box", box))
    for widget in (first, hidden, button, second, empty, rich, box):
        layout.addWidget(widget)
    hidden.hide()
    box.hide()
    root.show()
    qtbot.waitExposed(root)
    assert visible_text(root) == "First line\nSecond line\nBold words"

    # A line added while the window shows is read in its layout place at once, before the layout has shown it.
    late = QLabel("Late line", root)
    layout.insertWidget(1, late)
    assert not late.isVisible()
    assert visible_text(root) == "First line\nLate line\nSecond line\nBold words"


# ----------------------------------------------------------------- Home cards


class _Zotero:
    """Zotero is not installed. Nothing is started."""

    def status(self):
        return ZoteroStatus(exe=None, process_running=None, api=ApiCheck(API_NOT_RUNNING, "Zotero is not answering."))

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, "Zotero is not answering.")

    def start_zotero(self, exe):
        raise AssertionError("no test starts Zotero")

    def search_items(self, query):
        return []


def _tunnel_not_available(scope):
    return RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE)


def _tools_with_rows():
    rows = (
        ToolRow("Codex", "Meridian entries", "no", "unchecked", "Not in Codex's settings yet."),
        ToolRow("This computer", "Local Meridian server", YES, YES, "The local server answers."),
    )
    return ToolsStatus(rows=rows, message="Checked. No tool is started or stopped.", checked_at=datetime(2026, 10, 23, 14, 5))


def _never(*args, **kwargs):
    raise AssertionError("no test runs a start or a stop")


def _actions(store, asked=None):
    """The runner actions. Every confirmation answers No (and is recorded in ``asked``), so nothing is ever run."""

    def answer_no(parent, title, body, confirm_text):
        if asked is not None:
            asked.append(title)
        return False

    return RunnerActions(
        store,
        make_control=_never,
        confirm=answer_no,
        find_zotero=lambda: None,
        probe_zotero=lambda: ApiCheck(API_NOT_RUNNING, "not running"),
        start_zotero=lambda exe, log_path=None: (_ for _ in ()).throw(AssertionError("no test starts Zotero")),
        probe_local=lambda url: local_client.HEALTH_NOT_RUNNING,
    )


def _home(qtbot, tmp_path, *, probe=None, tools=None, core=False, asked=None) -> HomeTab:
    """Home with fakes. ``core`` sets a core checkout and Python (paths in tmp_path, never run), so the Tunnel and Local
    buttons are on and a press asks first; every question is answered No."""
    store = SettingsStore(tmp_path / "settings.json")
    if core:
        store.save(Settings(core_python=str(tmp_path / "python.exe"), core_checkout=str(tmp_path / "core")))
    deps = HomeDeps(
        store=store,
        probe=probe or (lambda url: local_client.HEALTH_NOT_RUNNING),
        tunnel=_tunnel_not_available,
        actions=_actions(store, asked),
        zotero=_Zotero(),
        tools=tools or _tools_with_rows,
        current_version="0.2.0",
    )
    tab = HomeTab(deps)
    qtbot.addWidget(tab)
    # In the test font the Local card's row of buttons is wider than half of 1100 pixels, so the cards would run past
    # the window's right edge. The window is as wide as both columns need, so a drag over a card stays in the window.
    cards = tab.findChild(QWidget, "home-cards")
    tab.resize(max(1100, cards.minimumSizeHint().width() + 80), 900)
    _activate(tab)
    return tab


def _card(tab, key) -> QFrame:
    card = tab.findChild(QFrame, f"home-card-{key}")
    assert card is not None, key
    return card


def _part(card, key, name) -> QLabel:
    return card.findChild(QLabel, f"home-{key}-{name}")


def _settled(qtbot, card, key, headline=None):
    def done():
        text = _part(card, key, "headline").text()
        checked = _part(card, key, "checked").text()
        if headline is not None:
            return text == headline and checked.startswith("Checked at")
        return text not in ("", CHECKING_HEADLINE) and checked.startswith("Checked at")

    qtbot.waitUntil(done, timeout=5000)
    _laid_out(qtbot, card, key)


def _all_settled(qtbot, tab) -> None:
    """Wait until every card that reads shows its answer, so no card above another still grows and moves it mid-drag."""
    for key in ("local", "tunnel", "services", "zotero", "tools"):
        _settled(qtbot, _card(tab, key), key)


def _laid_out(qtbot, card, key) -> None:
    """Wait until the card's layout has given its text block the height its text needs, so a drag lands on the lines."""
    block = card.findChild(QLabel, f"home-{key}-text")
    if block is not None:
        qtbot.waitUntil(lambda: block.height() == block.heightForWidth(block.width()), timeout=5000)


def _copy_action(widget):
    copies = [action for action in widget.actions() if action.text() == "Copy"]
    assert len(copies) == 1, widget.objectName()
    return copies[0]


def _block(card, key) -> QLabel:
    """The card's one text block: every line of the card, selectable in one drag."""
    block = card.findChild(QLabel, f"home-{key}-text")
    assert block is not None, key
    return block


def _plain(text: str) -> str:
    """A label's selected text as the clipboard gets it: paragraph and line breaks as newlines, no-break spaces as spaces."""
    return text.replace("\u2029", "\n").replace("\u2028", "\n").replace("\u00a0", " ")


def _shown_labels(card) -> list[QLabel]:
    """Every label the owner sees on the card, top to bottom, whoever built it: the card's own lines and the runner
    controls' note and result lines under the Tunnel and Local buttons alike."""
    shown = [label for label in card.findChildren(QLabel) if label.isVisible() and label.text()]
    shown.sort(key=lambda label: (label.mapTo(card, QPoint(0, 0)).y(), label.mapTo(card, QPoint(0, 0)).x()))
    return shown


def _first_and_last_line(card, key) -> tuple[QLabel, QLabel]:
    """The labels that show the card's first line (its title) and its last line, as they sit on the card."""
    shown = _shown_labels(card)
    return shown[0], shown[-1]


def _scroll_into_view(card) -> None:
    """Scroll Home so the whole card shows, as the owner does before dragging over it."""
    scroll = card.parent()
    while scroll is not None and not isinstance(scroll, QScrollArea):
        scroll = scroll.parent()
    scroll.ensureWidgetVisible(card, 0, 0)
    QApplication.processEvents()


def _drag_whole_card(window, card, key) -> None:
    """Press on the first character of the card's first line and release at the end of its last line."""
    _scroll_into_view(card)
    first, last = _first_and_last_line(card, key)
    end = QPoint(last.width() - 2, last.height() - 2)
    _drag(window, first, QPoint(1, 3), last, end)


CHECKED = r"Checked at \d\d:\d\d:\d\d\."

# For each block type, the lines a drag over the whole card copies, top to bottom. Each entry is a regular expression.
WHOLE_CARD_LINES = {
    # headline, detail and checked line, then the line under the local server's buttons (no core is set in the test)
    "local": ["Local Meridian server", "Not running", r"Nothing listens on port 7878\. .+", CHECKED, re.escape(CORE_NOT_SET_TEXT)],
    # the tunnel's buttons sit under its lines and are not copied; the line under them is
    "tunnel": ["Tunnel", "Not checked", r"The tunnel cannot be checked until .+", CHECKED, re.escape(CORE_NOT_SET_TEXT)],
    # rows without buttons, each with a detail line: the LaTeX row (row 0) and the slot ports
    "services": [
        "Services", "LaTeX backup engine off", CHECKED,
        r"LaTeX normally runs in Overleaf in your browser\. Local backup engine on port 8471\.",
        r"Nothing listens on that port\. The local engine is an optional backup .+ do not start this one\.",
        r"Slot ports: 0 of 1 listening on 127\.0\.0\.1\.", r"A listening port only shows .+",
    ],
    "zotero": ["Zotero", "Not installed", r"Zotero was not found .+", CHECKED],
    # rows with a button each
    "tools": [
        "AI clients and tools", "1 need attention", r"Checked\. No tool is started or stopped\.", CHECKED,
        r"Codex: Meridian entries\. Not configured\.", r"Not in Codex's settings yet\.",
        r"This computer: Local Meridian server\. Live\.", r"The local server answers\.",
    ],
    # no checked line: the Updates card reads the status bar's one check
    "updates": ["Updates", r"Version 0\.2\.0", r"Update checks are off\. .+"],
}


def _assert_lines(copied: str, expected: list[str]) -> None:
    lines = copied.split("\n")
    assert len(lines) == len(expected), lines
    for line, pattern in zip(lines, expected):
        assert re.fullmatch(pattern, line), (pattern, line)


@pytest.mark.parametrize("key", list(WHOLE_CARD_LINES))
def test_one_drag_from_the_first_line_to_the_last_selects_the_whole_card_and_ctrl_c_copies_it(qtbot, tmp_path, key):
    tab = _home(qtbot, tmp_path)
    # Every card has its answer, so no card above this one still grows and moves it.
    _all_settled(qtbot, tab)
    card = _card(tab, key)
    _laid_out(qtbot, card, key)

    _drag_whole_card(tab, card, key)
    copied = _ctrl_c(tab)

    _assert_lines(copied, WHOLE_CARD_LINES[key])
    block = _block(card, key)
    assert QApplication.focusWidget() is block
    assert _plain(block.selectedText()) == copied
    assert copied == visible_text(card), "the drag copies every line the card shows, as its Copy action does"
    # Every line the card shows is in its one block: no other label on the card shows text.
    assert _shown_labels(card) == [block]
    for button in card.findChildren(QPushButton):
        if button.text():
            assert button.text() not in copied.split("\n"), button.objectName()


@pytest.mark.parametrize(
    "note",
    [LAUNCH_CANCELLED_TEXT, f"Started Zotero.\n{API_NOT_READY_TEXT}"],
    ids=["one-line-note", "two-line-note"],
)
def test_a_card_note_is_part_of_the_one_drag(qtbot, tmp_path, note):
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "zotero")
    _settled(qtbot, card, "zotero")
    card.set_note(note)
    _laid_out(qtbot, card, "zotero")

    _drag_whole_card(tab, card, "zotero")
    copied = _ctrl_c(tab)

    _assert_lines(copied, WHOLE_CARD_LINES["zotero"] + [re.escape(line) for line in note.split("\n")])
    assert _part(card, "zotero", "note").text() == note


@pytest.mark.parametrize(("key", "prefix"), [("tunnel", "tunnel"), ("local", "home-local")])
def test_the_line_under_the_tunnel_and_local_buttons_is_part_of_the_one_drag(qtbot, tmp_path, key, prefix):
    asked = []
    tab = _home(qtbot, tmp_path, core=True, asked=asked)
    card = _card(tab, key)
    _all_settled(qtbot, tab)
    block = _block(card, key)
    start = card.findChild(QPushButton, f"{prefix}-start")
    assert start.isEnabled()
    # The core is set, so no line shows under the buttons yet, and the card's block ends with its buttons.
    _assert_lines(visible_text(card), WHOLE_CARD_LINES[key][:4])
    _laid_out(qtbot, card, key)
    _scroll_into_view(card)
    controls = card.findChild(QWidget, f"{prefix}-controls")
    assert controls.parentWidget() is card
    assert controls.geometry().top() > block.geometry().top() + _paragraph_box(block, 3).bottom()
    assert controls.geometry().bottom() == block.geometry().bottom()
    assert controls.geometry().left() == block.geometry().left() and controls.width() == block.width()
    detail = _part(card, key, "detail").text()
    _drag_over_paragraph(tab, block, 2)
    assert _plain(block.selectedText()) == detail

    # Start asks first. The answer is No, so nothing starts, and the line under the buttons says so. The line changes
    # while the detail is selected, and the detail stays selected. (click() leaves the focus where it is, as a start
    # check's later answer does. A real click on a button takes the focus, and the label's selection with it.)
    start.click()
    assert asked == [f"Start the {'tunnel' if key == 'tunnel' else 'local server'}?"]
    cancelled = CANCELLED_TEXT[START]
    assert card.findChild(QLabel, f"{prefix}-control-result").text() == cancelled
    assert _plain(block.selectedText()) == detail
    _laid_out(qtbot, card, key)

    _drag_whole_card(tab, card, key)
    copied = _ctrl_c(tab)

    _assert_lines(copied, WHOLE_CARD_LINES[key][:4] + [re.escape(cancelled)])
    assert copied == visible_text(card)
    assert _shown_labels(card) == [block]
    # The buttons still sit between the card's own lines and the line under them, and a real click on Start, where the
    # card shows it, still asks.
    block_top = block.mapTo(card, QPoint(0, 0)).y()
    start_top = start.mapTo(card, QPoint(0, 0)).y()
    assert block_top + _paragraph_box(block, 3).bottom() < start_top
    assert start_top + start.height() <= block_top + _paragraph_box(block, 4).top()
    _click(tab, start, start.rect().center())
    assert len(asked) == 2


def test_a_gate_change_redraws_the_line_under_the_buttons_and_keeps_the_card_as_wide_as_its_buttons(qtbot, tmp_path):
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "local")
    _all_settled(qtbot, tab)
    block = _block(card, "local")
    controls = card.findChild(QWidget, "home-local-controls")
    fix = card.findChild(QPushButton, "home-local-fix")
    assert fix.isVisible()
    assert visible_text(card).split("\n")[-1] == CORE_NOT_SET_TEXT
    wide = controls.minimumSizeHint().width()
    assert block.minimumWidth() == max(block.minimumSizeHint().width(), wide)

    # The core is set: the note under the buttons goes, the fix button with it, and the block keeps the buttons' width.
    SettingsStore(tmp_path / "settings.json").save(
        Settings(core_python=str(tmp_path / "python.exe"), core_checkout=str(tmp_path / "core"))
    )
    tab.refresh_tunnel_gate()
    assert not fix.isVisible()
    _assert_lines(visible_text(card), WHOLE_CARD_LINES["local"][:4])
    assert controls.minimumSizeHint().width() < wide
    assert block.minimumWidth() == max(block.minimumSizeHint().width(), controls.minimumSizeHint().width())
    _laid_out(qtbot, card, "local")
    assert controls.geometry().bottom() == block.geometry().bottom()


def test_a_drag_over_the_latex_row_selects_all_of_it_and_runs_on_to_the_last_line(qtbot, tmp_path):
    """The Services card's LaTeX row (row 0) is a line and a detail of several lines, in the card's one block."""
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "services")
    _all_settled(qtbot, tab)
    block = _block(card, "services")
    _scroll_into_view(card)
    # Paragraphs: title, headline, checked line, the LaTeX row and its detail, the slot ports row and its detail.
    document = block.findChild(QTextDocument)
    assert document.findBlockByNumber(4).layout().lineCount() > 1, "the LaTeX row's detail wraps"
    row, detail, last = (_paragraph_box(block, index) for index in (3, 4, 6))

    _drag(tab, block, QPoint(row.left() + 1, row.top() + 3), block, QPoint(detail.right() - 1, detail.bottom() - 2))
    _assert_lines(_ctrl_c(tab), WHOLE_CARD_LINES["services"][3:5])

    _click(tab, block, QPoint(block.width() - 2, _paragraph_box(block, 0).top() + 3))
    _drag(tab, block, QPoint(row.left() + 1, row.top() + 3), block, QPoint(last.right() - 1, last.bottom() - 2))
    _assert_lines(_ctrl_c(tab), WHOLE_CARD_LINES["services"][3:])


def test_row_buttons_sit_level_with_their_rows_at_the_right_and_a_click_runs_the_row_action(qtbot, tmp_path):
    reads = []

    def tools():
        reads.append(1)
        return _tools_with_rows()

    tab = _home(qtbot, tmp_path, tools=tools)
    card = _card(tab, "tools")
    _settled(qtbot, card, "tools", "1 need attention")
    block = _block(card, "tools")
    # The label's own document, as QLabel laid it out, places each paragraph. Rows start at paragraph 4.
    document = block.findChild(QTextDocument)
    assert document is not None
    layout = document.documentLayout()
    paragraphs = []
    paragraph = document.begin()
    while paragraph.isValid():
        paragraphs.append(paragraph)
        paragraph = paragraph.next()
    assert [p.text() for p in paragraphs[4:]] == [
        "Codex: Meridian entries. Not configured.",
        "Not in Codex's settings yet.",
        "This computer: Local Meridian server. Live.",
        "The local server answers.",
    ]
    block_box = block.geometry()
    for index, first_paragraph in ((0, paragraphs[4]), (1, paragraphs[6])):
        button = card.findChild(QPushButton, f"home-tools-row-{index}-action")
        assert button.isVisible()
        box = button.geometry()
        assert box.top() == block_box.top() + round(layout.blockBoundingRect(first_paragraph).top())
        assert box.right() == block_box.right()
        # The row's text keeps the button's width free, so no word runs under the button.
        assert first_paragraph.blockFormat().rightMargin() >= box.width()
    # The last row ends below its button, so the button stays on the card.
    last = card.findChild(QPushButton, "home-tools-row-1-action")
    assert last.geometry().bottom() <= block_box.bottom()

    # A real click on a row's button runs its action (Check again reads the tools once more).
    _scroll_into_view(card)
    _click(tab, last, last.rect().center())
    qtbot.waitUntil(lambda: len(reads) == 2, timeout=5000)


def test_a_home_card_copies_its_visible_text_from_the_right_click_menu(qtbot, tmp_path):
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "tools")
    _settled(qtbot, card, "tools", "1 need attention")
    assert card.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu
    # A right-click on the card's text opens the card's menu, so Copy is there wherever the owner clicks.
    block = _block(card, "tools")
    assert block.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu

    clipboard = QGuiApplication.clipboard()
    clipboard.setText(BEFORE)
    _copy_action(card).trigger()
    copied = clipboard.text()

    _assert_lines(copied, WHOLE_CARD_LINES["tools"])
    # Button text and hidden widgets are never copied.
    assert _part(card, "tools", "note").isHidden()
    assert copied == visible_text(card)

    # A right-click on a row's line opens the card's menu, and its Copy copies the whole card.
    assert _right_click_menu(qtbot, block) == [["Copy"]]
    assert clipboard.text() == copied


def _right_click_menu(qtbot, label: QLabel) -> list[list[str]]:
    """Right-click the label's last line, and press Copy in the menu that opens, when it has one. Returns the menu's
    actions. The mouse's context menu event goes to the label itself: offscreen QTest sometimes hands the one it makes
    from a click to the top-level widget instead. The probe in the report makes the real right-click through the window.
    """
    QGuiApplication.clipboard().setText(BEFORE)
    seen = []

    def use_open_menu():
        menu = QApplication.activePopupWidget()
        if menu is None:
            return
        seen.append([action.text() for action in menu.actions()])
        copy = next((action for action in menu.actions() if action.text() == "Copy"), None)
        if copy is not None:
            copy.trigger()
        menu.close()

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(use_open_menu)
    timer.start()
    point = QPoint(4, label.height() - 4)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, label.mapToGlobal(point))
    try:
        QApplication.sendEvent(label, event)  # the menu runs until the timer has used it
        qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    finally:
        timer.stop()
    return seen


@pytest.mark.parametrize("core", [False, True], ids=["note-under-the-buttons", "result-under-the-buttons"])
@pytest.mark.parametrize(("key", "prefix"), [("tunnel", "tunnel"), ("local", "home-local")])
def test_a_right_click_on_the_line_under_the_buttons_opens_the_card_menu(qtbot, tmp_path, key, prefix, core):
    tab = _home(qtbot, tmp_path, core=core)
    card = _card(tab, key)
    _all_settled(qtbot, tab)
    if core:
        card.findChild(QPushButton, f"{prefix}-start").click()  # asks, and the answer is No
    _laid_out(qtbot, card, key)
    last = _shown_labels(card)[-1]
    expected = CANCELLED_TEXT[START] if core else CORE_NOT_SET_TEXT
    assert visible_text(card).split("\n")[-1] == expected

    assert _right_click_menu(qtbot, last) == [["Copy"]]
    assert QGuiApplication.clipboard().text() == visible_text(card)


def test_ctrl_c_copies_the_whole_card_when_the_card_has_focus_and_only_the_selection_in_a_label(qtbot, tmp_path):
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "local")
    _settled(qtbot, card, "local", "Not running")
    corner = QPoint(card.width() - 6, card.height() - 4)
    assert card.childAt(corner) is None  # the card's own margin, not one of its lines
    _click(tab, card, corner)
    assert QApplication.focusWidget() is card
    copied = _ctrl_c(tab)
    assert copied == visible_text(card)
    _assert_lines(copied, WHOLE_CARD_LINES["local"])

    # Inside the text, Ctrl+C copies what was selected there, not the whole card.
    detail = _part(card, "local", "detail")
    _drag_over_paragraph(tab, _block(card, "local"), 2)
    assert QApplication.focusWidget() is _block(card, "local")
    assert _ctrl_c(tab) == detail.text()


def _paragraph_box(block: QLabel, index: int):
    """Where paragraph ``index`` sits in the block, from the label's own document as QLabel laid it out."""
    document = block.findChild(QTextDocument)
    paragraph = document.begin()
    for _ in range(index):
        paragraph = paragraph.next()
    return document.documentLayout().blockBoundingRect(paragraph).toAlignedRect()


def _drag_over_paragraph(window, block: QLabel, index: int) -> None:
    """Drag from the first character of one paragraph of the block to the end of its last line.

    A press inside a selection drags the selected text, as in any text field, so a click clears the selection first:
    at the right end of the title line, where no button sits (a runner's buttons sit at the block's foot).
    """
    title = _paragraph_box(block, 0)
    _click(window, block, QPoint(block.width() - 2, title.top() + 3))
    box = _paragraph_box(block, index)
    _drag(window, block, QPoint(box.left() + 1, box.top() + 3), block, QPoint(box.right() - 1, box.bottom() - 2))


def test_a_recheck_keeps_a_whole_card_selection_and_a_selection_of_one_line(qtbot, tmp_path):
    gate = threading.Event()
    calls = []

    def probe(url):
        calls.append(url)
        if len(calls) > 1:
            gate.wait(5)
        return local_client.HEALTH_NOT_RUNNING

    tab = _home(qtbot, tmp_path, probe=probe)
    card = _card(tab, "local")
    _settled(qtbot, card, "local", "Not running")
    block = _block(card, "local")
    _drag_whole_card(tab, card, "local")
    assert _plain(block.selectedText()) == visible_text(card)

    # The check runs: the checked line says Checking, and the whole card stays selected, the line under the buttons too.
    card.check()
    assert _part(card, "local", "headline").text() == "Not running"
    assert _part(card, "local", "checked").text() == CHECKING_HEADLINE
    assert visible_text(card).split("\n")[3:] == [CHECKING_HEADLINE, CORE_NOT_SET_TEXT]
    assert _plain(block.selectedText()) == visible_text(card)

    # Its answer comes back with a new time, and the whole card is still selected.
    gate.set()
    _settled(qtbot, card, "local", "Not running")
    assert len(calls) == 2
    assert _plain(block.selectedText()) == visible_text(card)
    _assert_lines(_ctrl_c(tab), WHOLE_CARD_LINES["local"])

    # A selection of one line above the checked line stays on that line through the next check.
    _drag_over_paragraph(tab, block, 2)
    detail = _part(card, "local", "detail").text()
    assert _plain(block.selectedText()) == detail
    card.check()
    _settled(qtbot, card, "local", "Not running")
    assert len(calls) == 3
    assert _plain(block.selectedText()) == detail
    assert _ctrl_c(tab) == detail


def test_new_label_text_keeps_a_selection_whose_text_is_still_there(qtbot):
    label = QLabel()
    qtbot.addWidget(label)
    label.setTextInteractionFlags(SELECTABLE_TEXT)
    label.setText("Status: ok.\nChecked at 10:00:00.\nLast line.")

    label.setSelection(label.text().index("Last"), len("Last line."))
    set_label_text(label, "Status: ok.\nChecked at 10:05:00 after a retry.\nLast line.")
    assert label.selectedText() == "Last line."  # after the change: moved with its text

    label.setSelection(label.text().index("10:05"), len("10:05:00"))
    set_label_text(label, "Status: ok.\nChecking...\nLast line.")
    assert not label.hasSelectedText(), "the selected words are gone, so nothing stays selected"

    label.setSelection(0, len("Status: ok."))
    set_label_text(label, "Status: ok.\nChecked at 10:06:00.\nLast line.")
    assert label.selectedText() == "Status: ok."  # before the change: kept where it was

    label.setSelection(label.text().index("Checked"), len("Checked at 10:06:00."))
    set_label_text(label, "Status: ok.\nChecked at 10:07:30.\nLast line.")
    assert label.selectedText() == "Checked at 10:07:30.", "a selected line whose time changed stays selected"

    label.setSelection(0, len(label.text()))
    set_label_text(label, "Status: down.\nChecked at 10:07:00.\nLast line.")
    assert _plain(label.selectedText()) == label.text(), "a selection round a change grows with it"


def test_paragraph_tops_and_text_bottom_follow_the_label_and_its_bottom_margin(qtbot):
    """A label drops its last paragraph's bottom margin, so room under a block is its bottom contents margin."""
    label = QLabel()
    qtbot.addWidget(label)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    font = label.font()
    label.setText(block_html([BlockLine("First line", space_after=10), BlockLine("Last line", space_after=30)], font))
    label.resize(300, label.heightForWidth(300))
    tops = paragraph_tops(label)
    assert len(tops) == 2 and tops[0] == 0 and tops[1] > 10
    bottom = text_bottom(label)
    assert bottom == label.heightForWidth(300), "the last paragraph's own bottom margin is left out"

    label.setContentsMargins(0, 0, 0, 30)
    label.resize(300, label.heightForWidth(300))
    assert paragraph_tops(label) == tops
    assert text_bottom(label) == bottom
    assert label.height() == bottom + 30


def test_unchanged_rows_keep_their_buttons_and_changed_rows_replace_the_old_ones(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    card = _Card("tools", "Tools", work=None, render=None, on_action=lambda key: None, parent=parent)
    QVBoxLayout(parent).addWidget(card)
    parent.show()
    qtbot.waitExposed(parent)

    first = CardView(headline="A", rows=(RowView("row a", action="x", action_label="Go"), RowView("row b")))
    card.show_view(first)
    button = card.findChild(QPushButton, "home-tools-row-0-action")
    assert button is not None and button.isVisible()
    card.show_view(first)
    _deferred_deletes()
    assert card.findChild(QPushButton, "home-tools-row-0-action") is button, "the same rows are not rebuilt"
    assert visible_text(card) == "Tools\nA\nrow a\nrow b"

    card.show_view(CardView(headline="B", rows=(RowView("row c"),)))
    assert visible_text(card) == "Tools\nB\nrow c"
    _deferred_deletes()
    assert card.findChildren(QPushButton, "home-tools-row-0-action") == []


# ----------------------------------------------------------------- Projects and Sessions tables


def _refuse(*args, **kwargs):
    raise AssertionError("not read in this test")


def test_the_projects_table_drags_over_several_rows_and_ctrl_c_copies_them(qtbot, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(mode="local"))
    rows = [
        {"id": f"id-{i}", "name": f"project-{i}", "status": "active", "priority": str(i), "created_at": f"2026-10-0{i + 1}"}
        for i in range(3)
    ]
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_RUNNING,
        read_server=lambda url: rows,
        read_file=_refuse,
        get_token=lambda: None,
        list_hosted=_refuse,
    )
    tab = ProjectsTab(store, sources)
    qtbot.addWidget(tab)
    tab.resize(1000, 500)
    _activate(tab)
    table = tab.findChild(QTableWidget, "projects-table")
    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    assert table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection

    viewport = table.viewport()
    _drag(tab, viewport, table.visualItemRect(table.item(0, 0)).center(), viewport, table.visualItemRect(table.item(2, 3)).center())
    assert sorted({item.row() for item in table.selectedItems()}) == [0, 1, 2]
    assert _ctrl_c(tab) == "\n".join(f"project-{i}\tactive\t{i}\t2026-10-0{i + 1}" for i in range(3))
    # The detail pane shows the first selected row.
    assert "Name: project-0" in tab.findChild(QLabel, "projects-detail").text()


class _Sessions:
    """A local transport that answers three sessions. No socket is opened."""

    def __init__(self):
        self.calls = 0

    def __call__(self, base_url, path, timeout):
        self.calls += 1
        rows = [
            {"id": f"s-{i}", "project_id": PID, "name": f"session-{i}", "status": "active",
             "last_seen": "2026-10-23T11:00:00+00:00", "client_type": "claude-code"}
            for i in range(3)
        ]
        return SimpleNamespace(status=200, body=json.dumps(rows).encode("utf-8"))


class _Clock(datetime):
    """Stands in for datetime inside sessions_panel only. Each reading is five seconds after the last."""

    readings = 0

    @classmethod
    def now(cls, tz=None):
        cls.readings += 1
        return datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=5 * cls.readings)


def test_the_sessions_table_copies_several_rows_and_a_tick_keeps_the_status_selection(qtbot, monkeypatch):
    monkeypatch.setattr(sessions_panel, "datetime", _Clock)
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
    panel._timer.stop()  # noqa: SLF001 - the test sends each tick itself
    for name in ("sessions-table", "sessions-claude-table"):
        assert panel.findChild(QTableWidget, name).selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection

    status = panel.findChild(QLabel, "sessions-status")
    checked = panel.findChild(QLabel, "sessions-checked")
    assert status.text() == "3 session(s) from this computer's Meridian server."
    assert checked.text().startswith("Checked at ")
    assert checked.textInteractionFlags() == SELECTABLE_TEXT
    _drag_over(panel, status)
    assert status.hasSelectedText()
    before = checked.text()

    panel._timer.timeout.emit()  # noqa: SLF001 - one 5-second tick
    qtbot.waitUntil(lambda: panel._in_flight is None and checked.text() != before, timeout=5000)  # noqa: SLF001
    assert local.calls == 2
    assert status.hasSelectedText(), "the tick changes only the time, which has its own line"
    assert _ctrl_c(panel) == status.text()

    viewport = table.viewport()
    _drag(panel, viewport, table.visualItemRect(table.item(0, 0)).center(), viewport, table.visualItemRect(table.item(2, 1)).center())
    assert sorted({item.row() for item in table.selectedItems()}) == [0, 1, 2]
    copied = _ctrl_c(panel).split("\n")
    assert [line.split("\t")[0] for line in copied] == ["session-0", "session-1", "session-2"]
    assert all(line.split("\t")[3] == "claude-code" for line in copied)


# ----------------------------------------------------------------- the status bar


class _Runner:
    def status(self, scope):
        return _tunnel_not_available(scope)


def _window(qtbot, tmp_path) -> MainWindow:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(mode="local"))

    class _Secrets:
        def __init__(self):
            self.values = {}

        def get_password(self, service, user):
            return self.values.get((service, user))

        def set_password(self, service, user, password):
            self.values[(service, user)] = password

        def delete_password(self, service, user):
            self.values.pop((service, user), None)

    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_refuse,
        read_file=_refuse,
        get_token=lambda: None,
        list_hosted=_refuse,
    )
    window = MainWindow(
        store,
        secret_store=KeyringSecretStore(backend=_Secrets()),
        data_sources=sources,
        zotero=_Zotero(),
        tunnel_runner=_Runner(),
        tools_status=lambda: ToolsStatus(rows=(), message="Not checked in this test."),
        update_transport=None,
        runner_actions=_actions(store),
    )
    qtbot.addWidget(window)
    return window


def test_the_status_bar_line_is_a_label_that_drags_and_copies(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    _activate(window)
    line = window.findChild(QLabel, "status-line")
    assert line is not None
    assert line.parent() is window.statusBar()
    assert line.textInteractionFlags() == SELECTABLE_TEXT
    assert line.text() == STATUS_MESSAGE == window.status_message()
    assert window.statusBar().currentMessage() == "", "no painted message hides the label"

    _drag_over(window, line)
    assert line.hasSelectedText()
    assert _ctrl_c(window) == STATUS_MESSAGE

    window.show_launch_notes("Launch note: nothing was started.")
    assert line.text() == "Launch note: nothing was started."
    assert window.statusBar().currentMessage() == ""


# ----------------------------------------------------------------- message boxes


def _copy_from_box(monkeypatch, wanted: str, record: dict) -> None:
    """Instead of waiting for an answer, show the box, drag over the line that says ``wanted``, press Ctrl+C, and close it.

    Nothing is clicked, so the box answers as a close does.
    """

    def run(box):
        _activate(box)
        label = next(label for label in box.findChildren(QLabel) if label.text() == wanted)
        record["flags"] = label.textInteractionFlags()
        _drag_over(box, label)
        record["selected"] = label.hasSelectedText()
        record["copied"] = _ctrl_c(box)
        box.close()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", run)


QUESTION = "Remove the RunPod key from this computer?\nThe key is deleted from the keychain."


@pytest.mark.parametrize("ask", [confirm.ask_yes_no, client_setup_section._ask_yes_no], ids=["confirm", "client-setup"])
def test_a_question_box_text_drags_and_copies_and_a_close_is_still_a_no(qtbot, monkeypatch, ask):
    record = {}
    _copy_from_box(monkeypatch, QUESTION, record)
    assert ask(None, "Meridian Desktop", QUESTION) is False
    assert record["flags"] & SELECTABLE_TEXT == SELECTABLE_TEXT
    assert record["selected"]
    assert record["copied"] == QUESTION


def test_the_first_run_notice_is_a_built_box_whose_text_drags_and_copies(qtbot, monkeypatch, tmp_path):
    notice = "Autostart could not be turned on.\nRun install.cmd, then start Meridian Desktop again."
    record = {}
    _copy_from_box(monkeypatch, notice, record)

    def static_box(*args, **kwargs):
        raise AssertionError("the static information box cannot be selected; build the box instead")

    class Stop(Exception):
        pass

    monkeypatch.setattr(QMessageBox, "information", static_box)
    monkeypatch.setattr(ui_app, "run_first_run", lambda store: FirstRunOutcome(settings=Settings(), notice=notice))
    # Neither the runner controls nor the window is built: the test stops main once the notice is closed.
    monkeypatch.setattr(ui_app, "RunnerActions", lambda *args, **kwargs: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        ui_app.main([], settings_path=tmp_path / "settings" / "settings.json")
    assert record["flags"] & SELECTABLE_TEXT == SELECTABLE_TEXT
    assert record["selected"]
    assert record["copied"] == notice
