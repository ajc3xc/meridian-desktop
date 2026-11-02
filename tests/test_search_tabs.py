"""The three search tabs: Code, Outputs and Research. Offscreen Qt, pytest-qt, fake transports only. No socket opens.

Each tab searches only its own kind, through its own route, and the status line names that kind. Each tab has its own
query box, results and status line. No search tab has a kind dropdown, and the window has seven tabs in order.
"""

import json
import threading

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QTableWidget

from meridian_desktop import local_client
from meridian_desktop.data_source import DataSources
from meridian_desktop.search_kinds import KINDS, STATE_EMPTY, KindOutcome, SearchOutcome
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import search_tab as search_tab_module
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow
from meridian_desktop.ui.search_tab import SEARCH_HINT, SearchTab

BASE = "http://127.0.0.1:7878"

CODE_BODY = {"results": [{"path": "meridian/server.py", "line": 7, "score": 1.0, "snippet": "def run():"}]}
OUTPUTS_BODY = {"hits": [{"path": "out/fig.png", "kind": "figure"}]}
RESEARCH_BODY = {"runs": [{"id": "run-x", "status": "done", "provider": "openalex"}]}
EMPTY_BODIES = {"code": {"results": []}, "outputs": {"hits": []}, "research": {"runs": []}}

# For each kind: its tab name, the route prefix it reads, and the one row its answer shows.
CASES = {
    "code": ("Code", "/desktop/code/search", CODE_BODY, ("Code", "meridian/server.py:7", "def run():")),
    "outputs": ("Outputs", "/desktop/outputs/search", OUTPUTS_BODY, ("Outputs", "out/fig.png", "figure")),
    "research": ("Research", "/desktop/research/runs", RESEARCH_BODY, ("Research", "run-x", "done; openalex")),
}


class Reply:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


class Route:
    """A transport for one route: records every (base URL, path) it is asked, and answers with ``reply``."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else Reply(404, b"")
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path))
        return self.reply


def _json(status, payload):
    return Reply(status, json.dumps(payload).encode("utf-8"))


def _tab(qtbot, kind, transports=None, url=BASE):
    tab = SearchTab(lambda: url, kind, transports=transports)
    qtbot.addWidget(tab)
    tab.show()
    qtbot.waitExposed(tab)
    return tab


def _search(tab, query):
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


def _all_text(widget):
    """Every string a user can read on this widget: labels, button labels and placeholder text."""
    texts = [label.text() for label in widget.findChildren(QLabel)]
    texts += [button.text() for button in widget.findChildren(QPushButton)]
    texts += [box.placeholderText() for box in widget.findChildren(QLineEdit)]
    return "\n".join(texts)


def _unexpected(*args):
    raise AssertionError("this data source should not be read in this test")


def _sources():
    """Fake data sources. No server answers, and no file, token or hosted call is read."""
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_unexpected,
        read_file=lambda path: [],
        get_token=lambda: None,
        list_hosted=_unexpected,
    )


def _window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), data_sources=_sources())
    qtbot.addWidget(window)
    return window


def _page(window, name):
    names = tuple(window.tabs.tabText(index) for index in range(window.tabs.count()))
    return window.tabs.widget(names.index(name))


# ----------------------------------------------------------- the window's tabs


def test_the_window_has_seven_tabs_in_order(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    names = tuple(window.tabs.tabText(index) for index in range(window.tabs.count()))
    assert names == ("Home", "Projects", "Sessions", "Code", "Outputs", "Research", "Settings")
    assert TAB_NAMES == names
    assert "Search" not in names


@pytest.mark.parametrize("kind", KINDS)
def test_each_search_tab_in_the_window_is_built_for_its_own_kind(qtbot, settings_path, kind):
    window = _window(qtbot, settings_path)
    page = _page(window, CASES[kind][0])
    assert isinstance(page, SearchTab)
    assert page.kind == kind
    assert page.objectName() == f"search-tab-{kind}"


@pytest.mark.parametrize("kind", KINDS)
def test_each_search_tab_in_the_window_searches_its_kind_at_the_saved_local_url(
    qtbot, settings_path, monkeypatch, kind
):
    SettingsStore(settings_path).save(Settings(mode="local", local_url="http://127.0.0.1:7999"))
    calls = []

    def fake_run_search(base_url, query, kinds, *, transports=None):
        calls.append((base_url, query, tuple(kinds)))
        outcome = KindOutcome(kinds[0], STATE_EMPTY, f'{CASES[kinds[0]][0]}: nothing matches "{query}".')
        return SearchOutcome(query=query, outcomes=(outcome,))

    monkeypatch.setattr(search_tab_module, "run_search", fake_run_search)
    window = _window(qtbot, settings_path)
    page = _page(window, CASES[kind][0])
    _search(page, "figure")
    _wait_done(qtbot, page)
    assert calls == [("http://127.0.0.1:7999", "figure", (kind,))]
    assert _status(page) == f'{CASES[kind][0]}: nothing matches "figure".'


# ----------------------------------------------------------- one kind per tab


def test_a_tab_takes_one_known_kind_and_refuses_any_other(qtbot):
    assert _tab(qtbot, "research").kind == "research"
    with pytest.raises(ValueError):
        SearchTab(lambda: BASE, "all")


@pytest.mark.parametrize("kind", KINDS)
def test_a_search_tab_runs_only_its_own_kind(qtbot, kind):
    _name, prefix, _body, row = CASES[kind]
    routes = {name: Route(_json(200, CASES[name][2])) for name in KINDS}
    tab = _tab(qtbot, kind, transports=routes)
    _search(tab, "run")
    qtbot.waitUntil(lambda: len(_rows(tab)) == 1, timeout=5000)
    _wait_done(qtbot, tab)
    assert len(routes[kind].calls) == 1
    assert routes[kind].calls[0][0] == BASE
    assert routes[kind].calls[0][1].startswith(prefix)
    assert _rows(tab) == [row]
    for other in KINDS:
        if other != kind:
            assert routes[other].calls == [], other


@pytest.mark.parametrize("kind", KINDS)
def test_the_status_line_names_the_tab_kind_for_results(qtbot, kind):
    tab = _tab(qtbot, kind, transports={kind: Route(_json(200, CASES[kind][2]))})
    _search(tab, "run")
    qtbot.waitUntil(lambda: len(_rows(tab)) == 1, timeout=5000)
    _wait_done(qtbot, tab)
    assert _status(tab).startswith(f'{CASES[kind][0]}: 1 match(es) for "run".')


@pytest.mark.parametrize("kind", KINDS)
def test_the_status_line_names_the_tab_kind_for_an_empty_answer(qtbot, kind):
    tab = _tab(qtbot, kind, transports={kind: Route(_json(200, EMPTY_BODIES[kind]))})
    _search(tab, "run")
    _wait_done(qtbot, tab)
    assert _rows(tab) == []
    assert _status(tab).startswith(f'{CASES[kind][0]}: nothing matches "run".')


def test_each_tab_keeps_its_own_query_results_and_status(qtbot):
    routes = {kind: Route(_json(200, CASES[kind][2])) for kind in KINDS}
    tabs = {kind: _tab(qtbot, kind, transports=routes) for kind in KINDS}

    _search(tabs["code"], "run")
    qtbot.waitUntil(lambda: len(_rows(tabs["code"])) == 1, timeout=5000)
    _wait_done(qtbot, tabs["code"])
    for other in ("outputs", "research"):
        assert _rows(tabs[other]) == [], other
        assert _status(tabs[other]) == SEARCH_HINT, other
        assert tabs[other].findChild(QLineEdit, "search-input").text() == "", other

    _search(tabs["outputs"], "figure")
    qtbot.waitUntil(lambda: len(_rows(tabs["outputs"])) == 1, timeout=5000)
    _wait_done(qtbot, tabs["outputs"])
    assert _rows(tabs["code"]) == [CASES["code"][3]]
    assert _status(tabs["code"]).startswith('Code: 1 match(es) for "run".')
    assert _rows(tabs["outputs"]) == [CASES["outputs"][3]]
    assert _status(tabs["outputs"]).startswith('Outputs: 1 match(es) for "figure".')
    assert _rows(tabs["research"]) == []
    assert _status(tabs["research"]) == SEARCH_HINT


def test_a_search_running_in_one_tab_does_not_hold_another(qtbot):
    gate = threading.Event()

    class Held(Route):
        def __call__(self, base_url, path, timeout):
            self.calls.append((base_url, path))
            gate.wait(5)
            return Reply(404, b"")

    code_tab = _tab(qtbot, "code", transports={"code": Held()})
    outputs_tab = _tab(qtbot, "outputs", transports={"outputs": Route(_json(200, OUTPUTS_BODY))})
    try:
        _search(code_tab, "slow")
        assert not code_tab.findChild(QPushButton, "search-button").isEnabled()
        _search(outputs_tab, "figure")
        qtbot.waitUntil(lambda: len(_rows(outputs_tab)) == 1, timeout=5000)
        _wait_done(qtbot, outputs_tab)
        assert _status(code_tab) == "Searching the local Meridian server..."
    finally:
        gate.set()
    _wait_done(qtbot, code_tab)
    assert _rows(code_tab) == []
    assert _status(code_tab) == "Not connected: the local server has no code search route yet"


# ----------------------------------------------------------- no kind dropdown


@pytest.mark.parametrize("kind", KINDS)
def test_a_search_tab_has_no_kind_dropdown_and_no_all_kinds_choice(qtbot, kind):
    tab = _tab(qtbot, kind)
    assert tab.findChildren(QComboBox) == []
    assert "All kinds" not in _all_text(tab)


def test_no_search_tab_in_the_window_has_a_kind_dropdown(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for name in ("Code", "Outputs", "Research"):
        page = _page(window, name)
        assert page.findChildren(QComboBox) == [], name
        assert "All kinds" not in _all_text(page), name
    assert window.findChildren(QComboBox, "search-kind") == []
