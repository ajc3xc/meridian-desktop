"""Every main tab scrolls: its content sits in one vertical QScrollArea, the mouse wheel moves it, and a left-button drag
that is held past the bottom (or top) edge of the viewport scrolls it while the selection runs on.

Each drag drives the window through its window handle, the way a real mouse does (see tests/test_copy_selection.py): a
press on a text line, moves with the button held, a hold past the edge, and a release. The window is small, so each
tab's content is taller than its viewport. Offscreen Qt, fakes only: no socket, no process, no real settings or keychain,
and no modal dialog waits for an answer. The Sessions tab would list the real processes, so its listing is replaced here.
"""

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QTextDocumentFragment, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QLabel, QScrollArea, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import claude_processes, local_client
from meridian_desktop.claude_processes import ClaudeProcessesResult
from meridian_desktop.data_source import DataSources
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tools_status import ToolsStatus
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus

LEFT = Qt.MouseButton.LeftButton
NO_KEYS = Qt.KeyboardModifier.NoModifier
# A small window, so each tab's content is taller than its viewport.
WINDOW_SIZE = (900, 360)
# Widgets of each main tab that must sit inside its scroll area, by object name. Home's cards are its scrolling part, and
# the Settings page is one scroll area already. The search tabs each have a results table of the same name, so they are
# looked up within their own tab.
CONTENT_PROBES = {
    "Home": ["home-cards"],
    "Projects": ["projects-status", "projects-table", "projects-detail"],
    "Sessions": ["sessions-intro", "sessions-table", "sessions-claude-table"],
    "Code": ["search-intro", "search-results"],
    "Outputs": ["search-intro", "search-results"],
    "Research": ["search-intro", "search-results"],
    "Settings": ["settings-page"],
}


def _refuse(*args, **kwargs):
    raise AssertionError("not read in this test")


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


class _Runner:
    """The tunnel's status call. The core is not set, so it reports not available and starts nothing."""

    def status(self, scope):
        return RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE)


def _never(*args, **kwargs):
    raise AssertionError("no test runs a start or a stop")


def _actions(store):
    """The runner actions. Every confirmation answers No, and nothing is started or chosen."""
    return RunnerActions(
        store,
        make_control=_never,
        confirm=lambda parent, title, body, confirm_text: False,
        find_zotero=lambda: None,
        probe_zotero=lambda: ApiCheck(API_NOT_RUNNING, "not running"),
        start_zotero=lambda exe, log_path=None: (_ for _ in ()).throw(AssertionError("no test starts Zotero")),
        probe_local=lambda url: local_client.HEALTH_NOT_RUNNING,
        locate=_never,
        choose_folder=lambda parent, current: "",
    )


@pytest.fixture(autouse=True)
def no_process_listing(monkeypatch):
    """The Sessions tab lists the Claude processes when it is shown. A test lists none, and reads no other process."""
    monkeypatch.setattr(
        claude_processes,
        "list_claude_processes",
        lambda *args, **kwargs: ClaudeProcessesResult(claude_processes.LISTED, "", ()),
    )


def _window(qtbot, tmp_path) -> MainWindow:
    """The real window with fakes. The local server is not running and nothing is saved to the keychain."""
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(mode="local"))
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_refuse,
        read_file=lambda path: [],
        get_token=lambda: None,
        list_hosted=_refuse,
    )
    window = MainWindow(
        store,
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        data_sources=sources,
        zotero=_Zotero(),
        tunnel_runner=_Runner(),
        tools_status=lambda: ToolsStatus(rows=(), message="Not checked in this test."),
        update_transport=None,
        runner_actions=_actions(store),
    )
    qtbot.addWidget(window)
    return window


def _shown(qtbot, tmp_path, name: str) -> MainWindow:
    """The window at the small size, shown and active, on the tab called ``name``."""
    window = _window(qtbot, tmp_path)
    window.resize(*WINDOW_SIZE)
    window.show()
    window.activateWindow()
    assert QTest.qWaitForWindowActive(window, 3000)
    window.tabs.setCurrentIndex(TAB_NAMES.index(name))
    QApplication.processEvents()
    return window


def _page(window: MainWindow, name: str) -> QWidget:
    """The widget the tab called ``name`` shows."""
    return window.tabs.widget(TAB_NAMES.index(name))


def _outer_scrolls(page: QWidget) -> list[QScrollArea]:
    """The scroll areas of a tab that no other scroll area of the tab is inside. The page counts when it is one."""
    areas = list(page.findChildren(QScrollArea))
    if isinstance(page, QScrollArea):
        areas.insert(0, page)
    outer = []
    for area in areas:
        parent, nested = area.parentWidget(), False
        while parent is not None:
            if isinstance(parent, QScrollArea):
                nested = True
                break
            parent = parent.parentWidget()
        if not nested:
            outer.append(area)
    return outer


def _the_scroll(window: MainWindow, name: str) -> QScrollArea:
    """The scroll area that holds ``name``'s content. A tab with none fails here, with the tab's name."""
    scrolls = _outer_scrolls(_page(window, name))
    assert scrolls, f"{name}: the content is in no scroll area, so the wheel and a drag cannot scroll it"
    return scrolls[0]


def _wheel_notch(target: QWidget, local: QPoint) -> None:
    """One notch of a mouse wheel at ``local`` in ``target``. The wheel goes to the widget, then to each widget above
    it in turn until one takes it, as the window hands a real wheel up the widget chain."""
    widget, point = target, local
    while widget is not None:
        event = QWheelEvent(
            QPointF(point),
            QPointF(widget.mapToGlobal(point)),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            NO_KEYS,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(widget, event)
        if event.isAccepted():
            return
        point = widget.mapToParent(point)
        widget = widget.parentWidget()


def _shown_text_lines(scroll: QScrollArea) -> list[QLabel]:
    """The selectable text lines in the scroll area's content that show, have text, and have a size."""
    lines = []
    for label in scroll.widget().findChildren(QLabel):
        if not label.isVisible() or not label.text() or label.width() == 0 or label.height() == 0:
            continue
        if label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse:
            lines.append(label)
    return lines


def _first_text_line(scroll: QScrollArea) -> QLabel:
    """The top-most text line that shows at the top of the scroll area's viewport."""
    viewport = scroll.viewport()
    tops = [(label.mapTo(viewport, QPoint(0, 0)).y(), label) for label in _shown_text_lines(scroll)]
    inside = [(top, label) for top, label in tops if 0 <= top < viewport.height() - 8]
    assert inside, "no text line shows at the top of the viewport"
    return min(inside, key=lambda item: item[0])[1]


def _last_text_line(scroll: QScrollArea) -> QLabel:
    """The text line whose end is lowest inside the scroll area's viewport."""
    viewport = scroll.viewport()
    ends = []
    for label in _shown_text_lines(scroll):
        bottom = label.mapTo(viewport, QPoint(0, label.height())).y()
        if 8 <= bottom <= viewport.height():
            ends.append((bottom, label))
    assert ends, "no text line ends inside the viewport"
    return max(ends, key=lambda item: item[0])[1]


def _plain(text: str) -> str:
    """A label's selected text as the clipboard gets it: paragraph and line breaks as newlines, no-break spaces as
    spaces. The separators are written as code points, so this file holds no invisible characters."""
    return text.replace(chr(0x2029), "\n").replace(chr(0x2028), "\n").replace(chr(0xA0), " ")


def _to_the_bottom(bar, name: str) -> None:
    """Scroll the viewport to its bottom, and check that it got there. A tab's maximum grows when its layout settles,
    a few events after it is shown, so the events run before the maximum is read. A stale maximum would leave the
    viewport a pixel or two down, and a drag back up would then prove nothing."""
    QApplication.processEvents()
    bar.setValue(bar.maximum())
    QApplication.processEvents()
    assert bar.value() == bar.maximum() > 0, f"{name}: the viewport is not at its bottom before the drag"


def _drag_past_an_edge(
    window: QWidget, scroll: QScrollArea, label: QLabel, *, down: bool, until_end: bool = True
) -> None:
    """Press on ``label`` and move past an edge of ``scroll``'s viewport: the bottom edge when ``down``, else the top.
    The press going down is on the label's first character. Going up it is at the right of the last line that shows in
    the viewport, past the end of short text, so the selection runs from the press back to the start of the text. Hold
    past the edge until the viewport reaches its end in that direction (or about five seconds pass), then release. With
    ``until_end`` false, release as soon as the viewport has moved off where it was when the drag began, so the release
    lands partway through the scroll. Every move and the release go through the window handle."""
    handle = window.windowHandle()
    viewport = scroll.viewport()
    bar = scroll.verticalScrollBar()
    start = bar.value()
    if down:
        begin = label.mapTo(window, QPoint(1, 3))
        past = viewport.mapTo(window, QPoint(min(60, viewport.width() - 2), viewport.height() + 25))
    else:
        shown = QRect(viewport.mapTo(window, QPoint(0, 0)), viewport.size()).intersected(
            QRect(label.mapTo(window, QPoint(0, 0)), label.size())
        )
        begin = QPoint(shown.right() - 3, shown.bottom() - 3)
        past = viewport.mapTo(window, QPoint(min(60, viewport.width() - 2), -25))
    # The moves first go to the inside of the edge, where the viewport does not scroll, and only then past it. So the
    # first scroll step comes after the mouse is past the edge, not during the moves.
    inside = viewport.mapTo(window, QPoint(min(60, viewport.width() - 2), viewport.height() - 2 if down else 1))
    QTest.mouseMove(handle, begin)
    QTest.mousePress(handle, LEFT, NO_KEYS, begin)
    for step in range(1, 13):
        QTest.mouseMove(
            handle,
            QPoint(begin.x() + (inside.x() - begin.x()) * step // 12, begin.y() + (inside.y() - begin.y()) * step // 12),
        )
        QApplication.processEvents()
    QTest.mouseMove(handle, past)
    QApplication.processEvents()
    end = bar.maximum() if down else bar.minimum()
    # The button is still held. The scroll goes on while the mouse stays past the edge, so hold until it gets to the end
    # (or, when the release is to land partway, until the viewport has moved at all).
    for _ in range(250):
        QTest.qWait(20)
        if bar.value() == end or (not until_end and bar.value() != start):
            break
    QTest.mouseRelease(handle, LEFT, NO_KEYS, past)
    QApplication.processEvents()


# ----------------------------------------------------------------- every tab's content is in one vertical scroll area


@pytest.mark.parametrize("name", TAB_NAMES)
def test_each_tab_content_sits_inside_one_vertical_scroll_area(qtbot, tmp_path, name):
    window = _window(qtbot, tmp_path)
    page = _page(window, name)
    outer = _outer_scrolls(page)
    assert len(outer) == 1, f"{name}: {len(outer)} scroll areas hold the tab's content, not one"
    scroll = outer[0]
    assert scroll.widgetResizable(), f"{name}: the scroll area does not resize its content to the viewport"
    assert scroll.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff, f"{name}: no vertical scroll bar"
    for probe in CONTENT_PROBES[name]:
        widget = page.findChild(QWidget, probe)
        assert widget is not None, f"{name}: no {probe!r} in the tab"
        assert scroll.isAncestorOf(widget), f"{name}: {probe!r} is not inside the tab's scroll area"


# ----------------------------------------------------------------- the mouse wheel

@pytest.mark.parametrize("name", TAB_NAMES)
def test_the_mouse_wheel_scrolls_each_tab(qtbot, tmp_path, name):
    window = _shown(qtbot, tmp_path, name)
    scroll = _the_scroll(window, name)
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"{name}: the content is not taller than the viewport at {WINDOW_SIZE}"
    label = _first_text_line(scroll)
    _wheel_notch(label, QPoint(label.width() // 2, label.height() // 2))
    QApplication.processEvents()
    assert bar.value() > 0, f"{name}: the wheel did not scroll the tab"


# The Settings connection section sits in a nested full-height scroll area (settings_tab._FullHeightScroll), as tall as
# its content, so it has nothing of its own to scroll. The wheel over it must go on to the Settings page, not stop there.
SETTINGS_CONNECTION_WIDGETS = ["settings-mode", "settings-hosted-url", "settings-test-connection"]


@pytest.mark.parametrize("widget_name", SETTINGS_CONNECTION_WIDGETS)
def test_the_wheel_over_the_settings_connection_section_scrolls_the_settings_page(qtbot, tmp_path, widget_name):
    window = _shown(qtbot, tmp_path, "Settings")
    scroll = _the_scroll(window, "Settings")
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"Settings: the content is not taller than the viewport at {WINDOW_SIZE}"
    assert bar.value() == 0
    target = _page(window, "Settings").findChild(QWidget, widget_name)
    assert target is not None, widget_name
    assert target.isVisible(), f"{widget_name} is not shown, so the wheel is not over the connection section"
    _wheel_notch(target, QPoint(target.width() // 2, target.height() // 2))
    QApplication.processEvents()
    assert bar.value() > 0, f"Settings: the wheel over {widget_name} did not scroll the Settings page"


def test_the_wheel_scrolls_the_settings_page_even_where_the_connection_section_can_scroll(qtbot, tmp_path):
    window = _shown(qtbot, tmp_path, "Settings")
    page = _page(window, "Settings")
    scroll = _the_scroll(window, "Settings")
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"Settings: the content is not taller than the viewport at {WINDOW_SIZE}"
    section = next(area for area in page.findChildren(QScrollArea, "settings-scroll") if area is not scroll)
    # Make the connection section shorter than its content, so it has rows of its own to scroll. The wheel must still
    # go to the Settings page, which is the design of the section: the page does the scrolling.
    section.setMaximumHeight(120)
    QApplication.processEvents()
    assert section.verticalScrollBar().maximum() > 0, "the connection section cannot scroll, so this checks nothing"
    target = page.findChild(QWidget, "settings-test-connection")
    assert target is not None
    _wheel_notch(target, QPoint(target.width() // 2, target.height() // 2))
    QApplication.processEvents()
    assert section.verticalScrollBar().value() == 0, "the connection section scrolled itself under the wheel"
    assert bar.value() > 0, "the wheel over the connection section did not scroll the Settings page"


# A table or a pane inside a tab, with nothing of its own to scroll, passes the wheel on to the tab.
NESTED_WHEEL_CASES = [
    ("Projects", "projects-table"),
    ("Projects", "projects-detail"),
    ("Sessions", "sessions-table"),
    ("Sessions", "sessions-claude-table"),
    ("Code", "search-results"),
    ("Outputs", "search-results"),
    ("Research", "search-results"),
]


@pytest.mark.parametrize(("name", "widget_name"), NESTED_WHEEL_CASES, ids=lambda case: str(case))
def test_a_wheel_over_a_table_or_pane_with_nothing_to_scroll_moves_the_tab(qtbot, tmp_path, name, widget_name):
    window = _shown(qtbot, tmp_path, name)
    scroll = _the_scroll(window, name)
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"{name}: the content is not taller than the viewport at {WINDOW_SIZE}"
    # Searched within the tab: the three search tabs each have a results table of the same name.
    target = _page(window, name).findChild(QWidget, widget_name)
    assert target is not None, widget_name
    if isinstance(target, QAbstractScrollArea):
        assert target.verticalScrollBar().maximum() == 0, f"{widget_name} has rows to scroll, so the wheel is its own"
        target = target.viewport()
    _wheel_notch(target, QPoint(target.width() // 2, target.height() // 2))
    QApplication.processEvents()
    assert bar.value() > 0, f"{name}: the wheel over {widget_name} did not move the tab"


# ----------------------------------------------------------------- a drag past the bottom edge


@pytest.mark.parametrize("name", TAB_NAMES)
def test_a_drag_past_the_bottom_edge_scrolls_each_tab_and_selects_to_the_end_of_the_line(qtbot, tmp_path, name):
    window = _shown(qtbot, tmp_path, name)
    scroll = _the_scroll(window, name)
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"{name}: the content is not taller than the viewport at {WINDOW_SIZE}"
    assert bar.value() == 0
    label = _first_text_line(scroll)

    _drag_past_an_edge(window, scroll, label, down=True)

    assert bar.value() == bar.maximum() > 0, f"{name}: the viewport did not scroll while the drag was held past the bottom"
    assert label.hasSelectedText(), f"{name}: the drag selected nothing"
    # The line's text is read after the drag: a tab that loads or checks while it is shown may change its text first.
    assert _plain(label.selectedText()).strip() == _line_plain(label).strip(), (
        f"{name}: the selection did not run on to the end of the line"
    )


@pytest.mark.parametrize("name", TAB_NAMES)
def test_a_drag_past_the_top_edge_scrolls_the_tab_back_up_and_selects_back_to_the_line_start(qtbot, tmp_path, name):
    window = _shown(qtbot, tmp_path, name)
    scroll = _the_scroll(window, name)
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"{name}: the content is not taller than the viewport at {WINDOW_SIZE}"
    _to_the_bottom(bar, name)
    label = _last_text_line(scroll)

    _drag_past_an_edge(window, scroll, label, down=False)

    assert bar.value() == 0, f"{name}: the viewport did not scroll back up while the drag was held past the top"
    assert label.hasSelectedText(), f"{name}: the drag selected nothing"
    # The selection runs from the press back to the start of the text, so it is the start of the line's text.
    selected = _plain(label.selectedText()).strip()
    assert label.selectionStart() == 0, f"{name}: the selection does not start at the line's first character"
    assert _line_plain(label).strip().startswith(selected), f"{name}: the selection did not run back to the line's start"


# The two tests above hold the drag until the viewport reaches its end, so the release lands at that end, and nothing
# can move after it. This one releases partway through the scroll, so a scroll that went on after the button came up
# would show, and the viewport must stay where the release found it.
@pytest.mark.parametrize("down", [True, False], ids=["bottom", "top"])
@pytest.mark.parametrize("name", TAB_NAMES)
def test_a_drag_released_partway_past_an_edge_stops_the_scroll_where_it_was_released(qtbot, tmp_path, name, down):
    window = _shown(qtbot, tmp_path, name)
    scroll = _the_scroll(window, name)
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, f"{name}: the content is not taller than the viewport at {WINDOW_SIZE}"
    if down:
        assert bar.value() == 0
        label = _first_text_line(scroll)
    else:
        _to_the_bottom(bar, name)
        label = _last_text_line(scroll)
    start = bar.value()
    edge = bar.maximum() if down else bar.minimum()

    _drag_past_an_edge(window, scroll, label, down=down, until_end=False)

    released = bar.value()
    side = "bottom" if down else "top"
    assert released != start, f"{name}: the viewport did not scroll while the drag was held past the {side}"
    assert released != edge, f"{name}: the viewport reached its end before the release, so the stop is not tested"
    _assert_scroll_stops_at_release(bar, name)


def _assert_scroll_stops_at_release(bar, name: str) -> None:
    """Once the button is up, the viewport stays where the drag left it."""
    settled = bar.value()
    QTest.qWait(150)
    assert bar.value() == settled, f"{name}: the viewport went on scrolling after the button was released"


def _line_plain(label: QLabel) -> str:
    """The text a label shows, as plain text: a rich-text block is read from its document."""
    if label.textFormat() == Qt.TextFormat.RichText:
        return _plain(QTextDocumentFragment.fromHtml(label.text()).toPlainText())
    return _plain(label.text())
