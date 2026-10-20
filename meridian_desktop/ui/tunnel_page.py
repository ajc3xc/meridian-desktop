"""Tunnel tab: one row per tunnel slot, from a snapshot handed in by the caller.

This is Qt code and sits in ui/, so the boundary check allows it. The page makes no request
and starts no timer. It shows the snapshot it is given (see meridian_desktop/tunnel_view.py for
the rows). Until a snapshot is given, it says the slot states are not available, because this
build has no local source for them. Every cell is plain text (QTableWidgetItem and PlainText
labels), so a slot name or note is never read as HTML.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.tunnel_view import slot_details, slots_of
from meridian_desktop.ui.text import enable_table_copy, make_selectable

NOT_AVAILABLE_TEXT = (
    "Tunnel slot states are not available. This build has no local source for them yet: "
    "the core does not write a tunnel status file the desktop can read."
)
NO_SLOTS_TEXT = "The tunnel snapshot lists no slots."
SHOWN_TEXT = "Slot states from the tunnel snapshot."
# The health columns come after Note, so the first three keep their positions.
SLOT_COLUMNS = ("Slot", "State", "Note", "Health basis", "Health age (s)")


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class TunnelPage(QWidget):
    """The Tunnel tab's slot table. It shows a snapshot it is given and has no request of its own."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tunnel-page")
        layout = QVBoxLayout(self)

        self._status = _label(self, "tunnel-status", NOT_AVAILABLE_TEXT)
        layout.addWidget(self._status)

        self._table = QTableWidget(0, len(SLOT_COLUMNS), self)
        self._table.setObjectName("tunnel-slot-table")
        self._table.setHorizontalHeaderLabels(list(SLOT_COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._table)
        layout.addWidget(self._table, 1)

    def set_snapshot(self, snapshot: Any) -> None:
        """Show one row per slot in ``snapshot``. None, or a snapshot with no slots dict, says not available.

        The page has no start, stop or restart control. It only shows what it is given.
        """
        details = slot_details(snapshot)
        if slots_of(snapshot) is None:
            self._status.setText(NOT_AVAILABLE_TEXT)
        elif not details:
            self._status.setText(NO_SLOTS_TEXT)
        else:
            self._status.setText(SHOWN_TEXT)

        self._table.setRowCount(len(details))
        for index, detail in enumerate(details):
            row = detail.row
            cells = (row.slot, row.state, row.note, detail.health_basis, detail.health_age)
            for column, text in enumerate(cells):
                self._table.setItem(index, column, QTableWidgetItem(text))


__all__ = [
    "NOT_AVAILABLE_TEXT",
    "NO_SLOTS_TEXT",
    "SHOWN_TEXT",
    "SLOT_COLUMNS",
    "TunnelPage",
]
