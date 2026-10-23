"""Helpers for the shared-delivery tests: the Delivered objects a pool job emits to shared_delivery().

A pool job emits each finished result as a Delivered to the one shared delivery object. A page keeps a Delivered only
when its owner token is its own and its generation (or job id) is current. Every Delivered reaches every page that is
connected to the shared object, so two pages of one kind see each other's answers.

The drop tests use two instances of a page. A sibling's answer carries another owner, and it is at the same generation
as this page's own answer, so the owner check alone must reject it. Replaying this page's earlier answer after a newer
read carries its own owner and a superseded generation, so the generation check alone must reject it. Nothing here
reads a private attribute of a page.
"""

from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget, QWidget

from meridian_desktop.ui.delivery import shared_delivery


@contextmanager
def recording_deliveries():
    """Yields a list that gains each object emitted to the shared delivery object while the block runs.

    The recorder is a direct connection, so it runs on the thread that emits, before the page's own handler is
    queued. A test can therefore read the list as soon as the page has shown a result.
    """
    emitted: list[object] = []

    def record(item: object) -> None:
        emitted.append(item)

    delivery = shared_delivery()
    delivery.delivered.connect(record, Qt.ConnectionType.DirectConnection)
    try:
        yield emitted
    finally:
        delivery.delivered.disconnect(record)


def emit(item: object) -> None:
    """Emit an object to the shared delivery object, as a pool job does."""
    shared_delivery().delivered.emit(item)


def visible_state(root: QWidget) -> tuple:
    """What a user sees under root: each label, button, line edit, plain-text box and table cell, in widget order."""
    state: list[tuple] = []
    for widget in root.findChildren(QWidget):
        if isinstance(widget, (QLabel, QPushButton)):
            state.append((widget.objectName(), widget.text(), widget.isEnabled()))
        elif isinstance(widget, QLineEdit):
            state.append((widget.objectName(), widget.text()))
        elif isinstance(widget, QPlainTextEdit):
            state.append((widget.objectName(), widget.toPlainText()))
        elif isinstance(widget, QTableWidget):
            cells = tuple(
                "" if widget.item(row, column) is None else widget.item(row, column).text()
                for row in range(widget.rowCount())
                for column in range(widget.columnCount())
            )
            state.append((widget.objectName(), widget.rowCount(), cells))
    return tuple(state)


def shows(state: tuple, text: str) -> bool:
    """True when text appears in any part of a visible_state result."""
    return any(text in str(part) for part in state)
