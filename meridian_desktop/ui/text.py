"""Read-only text in the desktop UI can be selected and copied.

Every label that shows text gets TextSelectableByMouse and TextSelectableByKeyboard, so a user
can highlight it and copy it. A read-only table gets a Copy action (Ctrl+C, and the right-click
menu) that copies the selected cells as tab-separated text. Editable fields are not touched.
"""

from __future__ import annotations

from typing import TypeVar

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QTableWidget

SELECTABLE_TEXT = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard

_TextWidget = TypeVar("_TextWidget", QLabel, QPlainTextEdit)


def make_selectable(widget: _TextWidget) -> _TextWidget:
    """Let the user select and copy the widget's text by mouse and keyboard. Returns the widget.

    For a QPlainTextEdit, call this after setReadOnly(True), which otherwise allows mouse selection only.
    """
    widget.setTextInteractionFlags(SELECTABLE_TEXT)
    return widget


def selected_cells_text(table: QTableWidget) -> str:
    """The selected cells: one line per row, cells separated by tabs. Empty when nothing is selected."""
    cells: dict[int, dict[int, str]] = {}
    for item in table.selectedItems():
        cells.setdefault(item.row(), {})[item.column()] = item.text()
    if not cells:
        return ""
    columns = sorted({column for row_cells in cells.values() for column in row_cells})
    return "\n".join(
        "\t".join(row_cells.get(column, "") for column in columns)
        for _row, row_cells in sorted(cells.items())
    )


def enable_table_copy(table: QTableWidget) -> None:
    """Ctrl+C and a right-click Copy action copy the table's selected cells to the clipboard."""
    action = QAction("Copy", table)
    action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def copy_selection() -> None:
        text = selected_cells_text(table)
        if text:
            QGuiApplication.clipboard().setText(text)

    action.triggered.connect(copy_selection)
    table.addAction(action)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
