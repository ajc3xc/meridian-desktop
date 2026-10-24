"""Read-only text in the desktop UI can be selected and copied.

Every label that shows text gets TextSelectableByMouse and TextSelectableByKeyboard, so a user
can highlight it and copy it. A read-only table gets a Copy action (Ctrl+C, and the right-click
menu) that copies the selected cells as tab-separated text. Editable fields are not touched.

A drag cannot cross from one label to the next, so a block made of several labels (a Home card)
gets its own Copy action that copies all the text it shows. A label whose text is set again to
the same text keeps its selection, so a redraw that changes nothing does not clear it. The labels
of a message box are made selectable the same way, because the windows11 style lets the owner
click only links in them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QTextDocumentFragment
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtWidgets import QLabel, QLayout, QMessageBox, QPlainTextEdit, QTableWidget, QWidget

SELECTABLE_TEXT = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
COPY_TEXT = "Copy"

_TextWidget = TypeVar("_TextWidget", QLabel, QPlainTextEdit)


def make_selectable(widget: _TextWidget) -> _TextWidget:
    """Let the user select and copy the widget's text by mouse and keyboard. Returns the widget.

    For a QPlainTextEdit, call this after setReadOnly(True), which otherwise allows mouse selection only.
    """
    widget.setTextInteractionFlags(SELECTABLE_TEXT)
    return widget


def set_label_text(label: QLabel, text: str) -> None:
    """Show ``text`` in the label. The same text again is left alone, so a selection in it stays."""
    if label.text() != text:
        label.setText(text)


def make_box_selectable(box: QMessageBox) -> QMessageBox:
    """Every label in the message box shows plain text the owner can select and copy. Call it once the text is set."""
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box


def _label_text(label: QLabel) -> str:
    text = label.text()
    if label.textFormat() == Qt.TextFormat.RichText or (
        label.textFormat() == Qt.TextFormat.AutoText and GuiQt.mightBeRichText(text)
    ):
        return QTextDocumentFragment.fromHtml(text).toPlainText()
    return text


def _hidden_on_purpose(widget: QWidget) -> bool:
    """True for a widget that hide() or setVisible(False) hid. A widget that a layout has not shown yet is not hidden."""
    return widget.isHidden() and widget.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide)


def _layout_labels(layout: QLayout, found: list[QLabel]) -> None:
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget()
        if widget is not None:
            _widget_labels(widget, found)
        elif item.layout() is not None:
            _layout_labels(item.layout(), found)


def _widget_labels(widget: QWidget, found: list[QLabel]) -> None:
    if _hidden_on_purpose(widget):
        return
    if isinstance(widget, QLabel):
        found.append(widget)
    elif widget.layout() is not None:
        _layout_labels(widget.layout(), found)


def visible_text(root: QWidget) -> str:
    """The text of every label in ``root``'s layout, one per line, in layout order: top to bottom, left to right.

    A label hidden on purpose (or inside a widget hidden on purpose), an empty label and the text of a button are left
    out. The order comes from the layouts, not from where the widgets are drawn, so lines a redraw has just added are
    read in their place even before the layout has placed and shown them.
    """
    found: list[QLabel] = []
    if root.layout() is not None:
        _layout_labels(root.layout(), found)
    return "\n".join(_label_text(label) for label in found if label.text())


def enable_widget_copy(widget: QWidget, text: Callable[[], str] | None = None) -> QAction:
    """A Copy action on ``widget``: in its right-click menu, and on Ctrl+C while the widget itself has focus.

    It copies ``text()``, by default visible_text(widget). The widget takes focus from a click on its own area or from
    Tab. Ctrl+C in a label inside it still copies only what is selected in that label.
    """
    read = text if text is not None else (lambda: visible_text(widget))
    action = QAction(COPY_TEXT, widget)
    action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def copy_all() -> None:
        copied = read()
        if copied:
            QGuiApplication.clipboard().setText(copied)

    action.triggered.connect(copy_all)
    widget.addAction(action)
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
    if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return action


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
    action = QAction(COPY_TEXT, table)
    action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def copy_selection() -> None:
        text = selected_cells_text(table)
        if text:
            QGuiApplication.clipboard().setText(text)

    action.triggered.connect(copy_selection)
    table.addAction(action)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
