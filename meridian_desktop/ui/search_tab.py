"""Search tabs for Code, Outputs and Research: one kind each, from the local Meridian server. Qt code, so it is in ui/.

Each tab has its own search box, results table and status line, and it searches only its own kind. The tab reads the
Local URL in Settings when a search runs, and sends the query to that address only. Each kind is one read of a route the
local server has (see meridian_desktop/search_kinds.py). A route the server does not have is shown as "Not connected",
and no result is made up.

A search runs only when Search or Enter is pressed, on the thread pool, so the window never blocks. A second press
while a search runs does nothing, and an answer from a superseded search is dropped. Results and messages are plain
text (PlainText labels and plain table cells), so server text is never read as HTML. The results table is read-only
and has the shared Copy action (Ctrl+C or the right-click menu).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.search_kinds import KIND_CODE, KIND_OUTPUTS, KIND_RESEARCH, KINDS, Row, SearchOutcome, run_search
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import submit
from meridian_desktop.ui.tab_scroll import MIN_TABLE_HEIGHT, let_the_wheel_pass, scrolled
from meridian_desktop.ui.text import enable_table_copy, make_selectable

# Each tab's intro and search-box placeholder, by kind. The Code intro is the owner's text, verbatim.
INTRO_TEXTS = {
    KIND_CODE: (
        "Search the code index of the local Meridian server, at the Local URL set in Settings. "
        "This tab contacts that address and nothing else."
    ),
    KIND_OUTPUTS: "Search the outputs the local Meridian server has saved.",
    KIND_RESEARCH: "Search the research sources the local Meridian server can reach.",
}
SEARCH_PLACEHOLDERS = {KIND_CODE: "Search code", KIND_OUTPUTS: "Search outputs", KIND_RESEARCH: "Search research"}
SEARCH_HINT = "Type a search term, then press Search or Enter."
SEARCHING_TEXT = "Searching the local Meridian server..."
BLANK_TEXT = "Type a search term first. Nothing was sent to the local Meridian server."
FAILED_TEXT = "The search could not be completed ({}). Press Search to try again."
RESULT_COLUMNS = ("Kind", "Where", "Details")


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _search_read(
    url_provider: Callable[[], str],
    kinds: tuple[str, ...],
    query: str,
    transports: Mapping[str, Any] | None,
) -> SearchOutcome:
    """Runs on a worker thread. The Local URL is read here, so the UI thread does no settings I/O."""
    return run_search(url_provider(), query, kinds, transports=transports)


def status_text(outcome: SearchOutcome) -> str:
    """One line per kind, in the order the kinds were searched. A line that is the same for every kind (the local server
    is not running) is shown once, so the same sentence does not repeat."""
    return "\n".join(dict.fromkeys(item.line for item in outcome.outcomes))


class SearchTab(QWidget):
    """One search tab for one kind: a search box, a Search button, a status line and one results table."""

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        kind: str,
        parent: QWidget | None = None,
        *,
        transports: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        if kind not in KINDS:
            raise ValueError(f"unknown search kind: {kind!r}")
        self._kind = kind
        self.setObjectName(f"search-tab-{kind}")
        self._url_provider = local_url_provider
        self._transports = dict(transports or {})
        self._searching = False
        # Identifies this tab's searches. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    @property
    def kind(self) -> str:
        """The one kind this tab searches: code, outputs or research."""
        return self._kind

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        # The content is one column in a vertical scroll area, so a short window scrolls the tab (see ui/tab_scroll.py).
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.addWidget(_label(self, "search-intro", INTRO_TEXTS[self._kind]))

        row = QHBoxLayout()
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("search-input")
        self._search_input.setPlaceholderText(SEARCH_PLACEHOLDERS[self._kind])
        self._search_button = QPushButton("Search", self)
        self._search_button.setObjectName("search-button")
        row.addWidget(self._search_input, 1)
        row.addWidget(self._search_button)
        layout.addLayout(row)

        self._status = _label(self, "search-status", SEARCH_HINT)
        layout.addWidget(self._status)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("search-results")
        # Short windows scroll the tab rather than squeeze the results to nothing (see ui/tab_scroll.py).
        self._results.setMinimumHeight(MIN_TABLE_HEIGHT)
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        self._results.horizontalHeader().setStretchLastSection(True)
        # A table with few results has nothing to scroll: the wheel goes on to the tab then (see ui/tab_scroll.py).
        let_the_wheel_pass(self._results)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scrolled(content, self))

        self._search_button.clicked.connect(lambda _checked=False: self._on_search_clicked())
        self._search_input.returnPressed.connect(self._on_search_clicked)

    # ----------------------------------------------------------- search

    @Slot()
    def _on_search_clicked(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            self._results.setRowCount(0)
            self._status.setText(BLANK_TEXT)
            return
        if self._searching:
            return
        self._searching = True
        self._generation += 1
        generation = self._generation
        self._search_button.setEnabled(False)
        self._results.setRowCount(0)
        self._status.setText(SEARCHING_TEXT)
        # The job holds the URL provider, this tab's one kind, the query, the transports, the owner token and the
        # shared delivery.
        work = partial(_search_read, self._url_provider, (self._kind,), query, self._transports)
        submit(self._owner, generation, work)

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab, or one a newer search superseded, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (value, failure) = item.value
        if generation != self._generation:
            return
        self._finish(value, failure)

    def _finish(self, value: object, failure: str | None) -> None:
        self._searching = False
        self._search_button.setEnabled(True)
        self._results.setRowCount(0)
        if failure is not None or not isinstance(value, SearchOutcome):
            self._status.setText(FAILED_TEXT.format(failure or "no result"))
            return
        self._fill_results(value.rows)
        self._status.setText(status_text(value))

    def _fill_results(self, rows: tuple[Row, ...]) -> None:
        self._results.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, text in enumerate((row.kind, row.where, row.details)):
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(index, column, cell)


__all__ = [
    "BLANK_TEXT",
    "INTRO_TEXTS",
    "SEARCH_HINT",
    "SEARCH_PLACEHOLDERS",
    "SearchTab",
    "status_text",
]
