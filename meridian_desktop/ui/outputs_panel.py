"""Outputs tab: search the local outputs index and read each hit's provenance, with the index state beside it.

This is Qt code and sits in ui/, so the boundary check allows it. The panel makes no request when it is
built. A search runs only when the owner presses Search or Enter. It runs on the thread pool, and the
server address comes from local_url_provider, which is also called on the pool, so the UI thread never
blocks. Results are plain text (PlainText labels and plain table cells), so the server's text is never
read as HTML. The results table is read-only and copyable: Ctrl+C or the right-click Copy action copies
the selected cells as tab-separated text.

The only control that sends a request is Search. The panel has no register, annotate or evict control.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
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

from meridian_desktop import outputs_client
from meridian_desktop.outputs_client import EMPTY, ERROR, INVALID, RESULTS, IndexState, OutputHit, OutputsResult
from meridian_desktop.ui.text import enable_table_copy, make_selectable

INTRO_TEXT = (
    "Outputs are the files Meridian has indexed from the outputs folder on this computer. "
    "Search by file name or content. The results come from the local Meridian server."
)
SEARCH_PLACEHOLDER = "Search outputs by file name or content"
SEARCH_HINT = "Type a search term, then press Search or Enter."
SEARCHING_TEXT = "Searching the outputs index..."
CHECK_FAILED_TEXT = "The search failed unexpectedly ({})."
INDEX_NOT_CHECKED_TEXT = "Index state: not checked yet. The server reports it with each search."
INDEX_NOT_REPORTED_TEXT = "Index state: the local server did not report it, so completeness is unknown."
INDEX_COMPLETE_TEXT = "Index state: complete."
INDEX_DEGRADED_TEXT = "Index state: incomplete, so results may be missing."
NOT_REPORTED = "not reported"
NO_CANONICAL = "none"
RESULT_COLUMNS = (
    "Path",
    "Score",
    "Kind",
    "Archival",
    "Canonical path",
    "Generating script",
    "Columns or keys",
    "Size (bytes)",
    "Modified",
)


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def index_text(index: IndexState | None, state: str = RESULTS) -> str:
    """The index line. A degraded index says why, so an incomplete result is never shown as complete."""
    if index is None:
        return INDEX_NOT_REPORTED_TEXT if state in (RESULTS, EMPTY) else INDEX_NOT_CHECKED_TEXT
    if not index.degraded:
        text = INDEX_COMPLETE_TEXT
        if index.last_rebuilt_at:
            text += f" Last rebuilt: {index.last_rebuilt_at}."
        return text
    reasons: list[str] = []
    if index.partial:
        reasons.append("the last rebuild ran out of time")
    if index.pending_count:
        reasons.append(f"{index.pending_count} file(s) are still queued")
    if index.walk_error_count:
        reasons.append(f"{index.walk_error_count} folder(s) could not be read")
    if index.db_write_failed:
        reasons.append("some index rows could not be saved")
    if not reasons:
        reasons.append("the server reports the index as degraded")
    return f"{INDEX_DEGRADED_TEXT} Reasons: {'; '.join(reasons)}."


def hit_row(hit: OutputHit) -> tuple[str, ...]:
    """The table cells for one hit, in RESULT_COLUMNS order. Missing provenance reads as 'not reported'."""
    return (
        hit.path,
        NOT_REPORTED if hit.score is None else f"{hit.score:.3f}",
        hit.kind or NOT_REPORTED,
        "yes" if hit.is_archival else "no",
        hit.canonical_path or NO_CANONICAL,
        hit.generating_script or NOT_REPORTED,
        ", ".join(hit.csv_columns or hit.json_keys) or NOT_REPORTED,
        NOT_REPORTED if hit.size is None else str(hit.size),
        hit.modified or NOT_REPORTED,
    )


class _Job(QRunnable):
    """Run ``work`` on the thread pool and deliver (result, failure_text).

    Any exception is reduced to its class name, so no traceback or raw detail reaches the UI.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        result: Any = None
        failure: str | None = None
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = CHECK_FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((result, failure))
        except RuntimeError:
            pass  # the panel was deleted while the search ran


class OutputsPanel(QWidget):
    """The Outputs tab: a search box, the index state, and a read-only, copyable list of hits."""

    _search_finished = Signal(object)

    def __init__(self, local_url_provider: Callable[[], str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("outputs-panel")
        self._local_url_provider = local_url_provider
        self._searching = False
        self._search_finished.connect(self._on_search_finished)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "outputs-intro", INTRO_TEXT))

        search_row = QHBoxLayout()
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("outputs-search-input")
        self._search_input.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search_button = QPushButton("Search", self)
        self._search_button.setObjectName("outputs-search-button")
        search_row.addWidget(self._search_input, 1)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        self._status = _label(self, "outputs-status", SEARCH_HINT)
        layout.addWidget(self._status)
        self._index = _label(self, "outputs-index", INDEX_NOT_CHECKED_TEXT)
        layout.addWidget(self._index)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("outputs-results")
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        self._results.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        self._search_button.clicked.connect(self._on_search_clicked)
        self._search_input.returnPressed.connect(self._on_search_clicked)

    # ----------------------------------------------------------- search

    @Slot()
    def _on_search_clicked(self) -> None:
        if self._searching:
            return
        text, problem = outputs_client.check_query(self._search_input.text())
        if problem is not None:
            self._show(OutputsResult(INVALID, problem, query=text))
            return
        self._searching = True
        self._search_button.setEnabled(False)
        self._status.setText(SEARCHING_TEXT)
        self._index.setText(INDEX_NOT_CHECKED_TEXT)
        self._results.setRowCount(0)
        QThreadPool.globalInstance().start(_Job(lambda: self._search_work(text), self._search_finished.emit))

    def _search_work(self, text: str) -> OutputsResult:
        """Read the server address from the provider, then search. Runs on a worker thread."""
        url = self._local_url_provider()
        return outputs_client.search_outputs(url, text)

    @Slot(object)
    def _on_search_finished(self, payload: tuple) -> None:
        result, failure = payload
        self._searching = False
        self._search_button.setEnabled(True)
        if failure is not None or not isinstance(result, OutputsResult):
            self._show(OutputsResult(ERROR, failure or CHECK_FAILED_TEXT.format("NoResult")))
            return
        self._show(result)

    def _show(self, result: OutputsResult) -> None:
        self._status.setText(result.message)
        self._index.setText(index_text(result.index, result.state))
        self._results.setRowCount(len(result.hits))
        for row, hit in enumerate(result.hits):
            for column, value in enumerate(hit_row(hit)):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(row, column, cell)


__all__ = [
    "INDEX_COMPLETE_TEXT",
    "INDEX_DEGRADED_TEXT",
    "INDEX_NOT_CHECKED_TEXT",
    "INDEX_NOT_REPORTED_TEXT",
    "OutputsPanel",
    "hit_row",
    "index_text",
]
