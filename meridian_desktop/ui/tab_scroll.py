"""Scrolling for the main tabs: each tab's content sits in a vertical scroll area, a wheel that a table or pane cannot
move goes on to the tab, and a left-button drag held past the bottom (or top) edge of the tab scrolls it while the
selection runs on. Qt code, so it is in ui/.

A text line (a QLabel) takes the mouse for the whole drag, so the selection follows the mouse only inside the line. Qt
does not scroll the area around the line when the mouse goes past its edge, so the drag would stop at the edge. The
drag scroll (_DragScroll) does that: while a left-button drag that began on a text line is held past an edge of a
scroll area around the line, it scrolls that area a step every few milliseconds, and then sends the line a move at the
mouse's place, so the selection runs on to the text now under the mouse. It acts on one scroll area at a time: the
nearest one above the line that can still move in that direction. A table scrolls its own rows (Qt's own drag
autoscroll), so a drag from a table is left alone.
"""

from __future__ import annotations

from collections.abc import Iterator

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QFrame, QLabel, QScrollArea, QWidget
from shiboken6 import isValid

# The name of the scroll area each tab's content is in (see scrolled).
TAB_SCROLL_NAME = "tab-scroll"
# The shortest a table is kept when its tab is short, in pixels. A table this tall shows its header and a few rows, and
# the tab scrolls instead of squeezing the table to nothing.
MIN_TABLE_HEIGHT = 160
# A drag scroll step: the viewport moves this many pixels every interval, plus a part of the mouse's distance past the
# edge, capped, so a drag far past the edge moves faster than one just past it.
DRAG_SCROLL_INTERVAL_MS = 30
DRAG_SCROLL_STEP_MIN = 4
DRAG_SCROLL_STEP_MAX = 40
DRAG_SCROLL_DISTANCE_DIVISOR = 3

LEFT = Qt.MouseButton.LeftButton
_NO_KEYS = Qt.KeyboardModifier.NoModifier


def scrolled(content: QWidget, parent: QWidget) -> QScrollArea:
    """``content`` in a vertical scroll area under ``parent``. The content is as wide as the area, and at least as tall
    as its own minimum height, so the area scrolls when the window is shorter than the content."""
    scroll = QScrollArea(parent)
    scroll.setObjectName(TAB_SCROLL_NAME)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


class _WheelPassOnAtEnd(QObject):
    """The filter behind let_the_wheel_pass. It belongs to the scroll area it watches, which is its parent."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if event.type() != QEvent.Type.Wheel or not isinstance(event, QWheelEvent):
            return False
        turn = event.angleDelta().y() or event.pixelDelta().y()
        if turn == 0:
            return False  # a sideways wheel is the area's own
        bar = self.parent().verticalScrollBar()
        at_end = bar.value() >= bar.maximum() if turn < 0 else bar.value() <= bar.minimum()
        if not at_end:
            return False
        event.ignore()  # not taken, so the window hands it up to the scroll area around this one
        return True


def let_the_wheel_pass(area: QAbstractScrollArea) -> None:
    """Let a wheel that ``area`` cannot move any further, in the direction it turns, go on to the scroll area around it.

    A scroll area takes every wheel event, even one that moves nothing, so a table or a pane with nothing to scroll
    would hold the wheel back from the tab around it. The filter on the area and on its viewport ignores the event only
    where the area is at its end in that direction. The area still scrolls when it can.
    """
    passer = _WheelPassOnAtEnd(area)
    area.installEventFilter(passer)
    area.viewport().installEventFilter(passer)


def _scroll_areas_above(widget: QWidget) -> Iterator[QScrollArea]:
    """Every scroll area that contains ``widget``, nearest first."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea):
            yield parent
        parent = parent.parentWidget()


def _scroll_toward(line: QLabel, where: QPoint) -> bool:
    """Scroll the nearest scroll area above ``line`` that can move toward ``where``, by one step. False when the mouse
    is inside every such area, or none of them can move further that way."""
    for scroll in _scroll_areas_above(line):
        viewport = scroll.viewport()
        top = viewport.mapToGlobal(QPoint(0, 0)).y()
        bottom = top + viewport.height()
        bar = scroll.verticalScrollBar()
        if where.y() > bottom:
            direction, distance, room = 1, where.y() - bottom, bar.maximum() - bar.value()
        elif where.y() < top:
            direction, distance, room = -1, top - where.y(), bar.value() - bar.minimum()
        else:
            continue
        if room <= 0:
            continue
        step = min(DRAG_SCROLL_STEP_MAX, DRAG_SCROLL_STEP_MIN + distance // DRAG_SCROLL_DISTANCE_DIVISOR)
        bar.setValue(bar.value() + direction * min(step, room))
        return True
    return False


class _DragScroll(QObject):
    """The application's event filter that scrolls a tab while a left-button drag from a text line is held past an edge.

    The press and the moves reach the filter before the line gets them. A press on a text line starts a drag, and any
    other press ends the last one. A release ends the drag, and so does a line deleted during it.
    """

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._line: QLabel | None = None
        self._where: QPoint | None = None
        self._relaying = False
        self._timer = QTimer(self)
        self._timer.setInterval(DRAG_SCROLL_INTERVAL_MS)
        self._timer.timeout.connect(self._scroll_step)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if self._relaying or not isinstance(event, QMouseEvent):
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == LEFT:
            self._stop()
            if isinstance(watched, QLabel):
                self._line = watched
                self._where = event.globalPosition().toPoint()
        elif kind == QEvent.Type.MouseMove and self._line is not None and event.buttons() & LEFT:
            self._where = event.globalPosition().toPoint()
            if not self._timer.isActive():
                self._timer.start()
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == LEFT:
            self._stop()
        return False

    def _scroll_step(self) -> None:
        line, where = self._line, self._where
        try:
            if line is None or where is None or not line.isVisible():
                self._stop()
                return
            moved = _scroll_toward(line, where)
            if moved:
                self._relay(line, where)
        except RuntimeError:  # the line was deleted during the drag
            self._stop()
            return
        if not moved:
            self._timer.stop()

    def _relay(self, line: QLabel, where: QPoint) -> None:
        """Send the line a move to the mouse's place, so its selection runs on to the text now under the mouse."""
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(line.mapFromGlobal(where)),
            QPointF(line.window().mapFromGlobal(where)),
            QPointF(where),
            Qt.MouseButton.NoButton,
            LEFT,
            _NO_KEYS,
        )
        self._relaying = True
        try:
            QApplication.sendEvent(line, move)
        finally:
            self._relaying = False

    def _stop(self) -> None:
        self._timer.stop()
        self._line = None
        self._where = None


_DRAG_SCROLL: _DragScroll | None = None


def install_drag_scroll() -> None:
    """Install the drag scroll on the application, once. A second call changes nothing."""
    global _DRAG_SCROLL  # noqa: PLW0603 - one filter for the whole application
    app = QApplication.instance()
    if app is None or (_DRAG_SCROLL is not None and isValid(_DRAG_SCROLL)):
        return
    _DRAG_SCROLL = _DragScroll(app)
    app.installEventFilter(_DRAG_SCROLL)
