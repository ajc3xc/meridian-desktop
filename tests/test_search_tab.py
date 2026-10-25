"""The search tabs: one box and one kind per tab, queries only to the Local URL, and no invented results.

Offscreen Qt, pytest-qt, fake transports only. No test opens a socket. The Local URL is read at the moment a search runs.
"""

import json
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTableWidget

from meridian_desktop import local_client
from meridian_desktop.ui.search_tab import BLANK_TEXT, SEARCH_HINT, SearchTab
from meridian_desktop.ui.text import SELECTABLE_TEXT

VERBATIM_INTRO = (
    "Search the code index of the local Meridian server, at the Local URL set in Settings. "
    "This tab contacts that address and nothing else."
)
# The intro and the search-box placeholder each tab shows, as the owner wrote them.
TAB_TEXT = {
    "code": (VERBATIM_INTRO, "Search code"),
    "outputs": ("Search the outputs the local Meridian server has saved.", "Search outputs"),
    "research": ("Search the research sources the local Meridian server can reach.", "Search research"),
}
NOT_RUNNING_SENTENCE = (
    "Not connected: the local Meridian server is not running at http://127.0.0.1:7878. "
    "Start Meridian on this computer, then search again."
)


class Reply:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


class Route:
    """A transport for one kind: records every (base URL, path) it is asked, and answers with ``reply``."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else Reply(404, b"")
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path))
        return self.reply


def _json(status, payload):
    return Reply(status, json.dumps(payload).encode("utf-8"))


def _tab(qtbot, kind="code", url="http://127.0.0.1:7878", transports=None):
    tab = SearchTab(lambda: url, kind, transports=transports)
    qtbot.addWidget(tab)
    tab.show()
    qtbot.waitExposed(tab)
    return tab


def _search(qtbot, tab, query):
    tab.findChild(QLineEdit, "search-input").setText(query)
    tab.findChild(QPushButton, "search-button").click()


def _status(tab):
    return tab.findChild(QLabel, "search-status").text()


def _rows(tab):
    table = tab.findChild(QTableWidget, "search-results")
    return [
        tuple(table.item(row, column).text() for column in range(table.columnCount()))
        for row in range(table.rowCount())
    ]


def _wait_done(qtbot, tab):
    button = tab.findChild(QPushButton, "search-button")
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)


@pytest.mark.parametrize("kind", ["code", "outputs", "research"])
def test_each_tab_shows_its_own_intro_and_placeholder(qtbot, kind):
    tab = _tab(qtbot, kind=kind)
    intro_text, placeholder = TAB_TEXT[kind]
    intro = tab.findChild(QLabel, "search-intro")
    assert intro.text() == intro_text
    assert intro.textInteractionFlags() == SELECTABLE_TEXT
    assert tab.findChild(QLineEdit, "search-input").placeholderText() == placeholder


def test_nothing_is_sent_until_search_is_pressed(qtbot):
    code = Route()
    tab = _tab(qtbot, transports={"code": code})
    qtbot.wait(20)
    assert code.calls == []
    tab.findChild(QLineEdit, "search-input").setText("typing only")
    qtbot.wait(20)
    assert code.calls == []
    assert _status(tab) == SEARCH_HINT


def test_a_blank_search_says_so_and_sends_nothing(qtbot):
    code = Route()
    tab = _tab(qtbot, transports={"code": code})
    _search(qtbot, tab, "   ")
    assert _status(tab) == BLANK_TEXT
    assert code.calls == []


def test_the_code_tab_searches_only_code_at_the_local_url_and_shows_one_line(qtbot):
    code = Route(_json(200, {"results": [{"path": "meridian/server.py", "line": 7, "score": 1.0, "snippet": "def run():"}]}))
    outputs = Route(_json(200, {"hits": [{"path": "out/fig.png", "kind": "figure"}]}))
    research = Route(_json(200, {"runs": [{"id": "run-x", "status": "done", "provider": "openalex"}]}))
    tab = _tab(
        qtbot,
        kind="code",
        url="http://127.0.0.1:7999",
        transports={"code": code, "outputs": outputs, "research": research},
    )
    _search(qtbot, tab, "run")
    qtbot.waitUntil(lambda: len(_rows(tab)) == 1, timeout=5000)
    _wait_done(qtbot, tab)
    assert code.calls == [("http://127.0.0.1:7999", code.calls[0][1])]
    assert code.calls[0][1].startswith("/desktop/code/search?")
    assert outputs.calls == [] and research.calls == []
    assert _rows(tab) == [("Code", "meridian/server.py:7", "def run():")]
    assert _status(tab) == 'Code: 1 match(es) for "run".'


@pytest.mark.parametrize("kind", ["code", "outputs", "research"])
def test_a_kind_whose_route_is_missing_says_not_connected_and_shows_no_rows(qtbot, kind):
    tab = _tab(qtbot, kind=kind, transports={kind: Route()})
    _search(qtbot, tab, "anything")
    _wait_done(qtbot, tab)
    assert _rows(tab) == []
    assert _status(tab) == f"Not connected: the local server has no {kind} search route yet"


@pytest.mark.parametrize("kind", ["code", "outputs", "research"])
def test_when_nothing_listens_the_not_running_sentence_is_shown_once(qtbot, kind):
    """Checked for each tab: the tab searches its one kind, and its status holds the sentence once and no other line."""

    def down(base_url, path, timeout):
        raise local_client.LocalClientError("not_running", "nothing listens")

    tab = _tab(qtbot, kind=kind, transports={kind: down})
    _search(qtbot, tab, "anything")
    _wait_done(qtbot, tab)
    status = _status(tab)
    assert chr(10) not in status
    assert status.count(NOT_RUNNING_SENTENCE) == 1
    assert status == NOT_RUNNING_SENTENCE


def test_enter_in_the_box_starts_the_search(qtbot):
    code = Route(_json(200, {"results": []}))
    tab = _tab(qtbot, transports={"code": code})
    box = tab.findChild(QLineEdit, "search-input")
    box.setText("enter-key")
    box.setFocus()
    QTest.keyClick(box, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(code.calls) == 1, timeout=5000)
    _wait_done(qtbot, tab)


def test_the_search_button_is_off_while_a_search_runs(qtbot):
    release = []

    class Slow(Route):
        def __call__(self, base_url, path, timeout):
            gate = threading.Event()
            release.append(gate)
            gate.wait(5)
            return Reply(404, b"")

    tab = _tab(qtbot, transports={"code": Slow(), "outputs": Route(), "research": Route()})
    button = tab.findChild(QPushButton, "search-button")
    _search(qtbot, tab, "slow")
    assert not button.isEnabled()
    assert _status(tab) == "Searching the local Meridian server..."
    qtbot.waitUntil(lambda: len(release) == 1, timeout=5000)
    release[0].set()
    _wait_done(qtbot, tab)


@pytest.mark.parametrize("kind,label", [("code", "Code"), ("outputs", "Outputs"), ("research", "Research")])
def test_a_non_loopback_local_url_sends_nothing_and_says_why(qtbot, kind, label):
    code = Route(_json(200, {"results": []}))
    outputs = Route(_json(200, {"hits": []}))
    research = Route(_json(200, {"runs": []}))
    tab = _tab(qtbot, kind=kind, url="http://example.com", transports={"code": code, "outputs": outputs, "research": research})
    _search(qtbot, tab, "anything")
    _wait_done(qtbot, tab)
    assert code.calls == [] and outputs.calls == [] and research.calls == []
    status = _status(tab)
    assert chr(10) not in status
    assert status.startswith(f"{label}: the search could not be completed.")


def test_the_table_cannot_be_edited_and_every_label_is_selectable(qtbot):
    tab = _tab(qtbot)
    table = tab.findChild(QTableWidget, "search-results")
    assert table.editTriggers() == table.EditTrigger.NoEditTriggers
    for label in tab.findChildren(QLabel):
        if label.text():
            assert label.textInteractionFlags() == SELECTABLE_TEXT, label.objectName()
