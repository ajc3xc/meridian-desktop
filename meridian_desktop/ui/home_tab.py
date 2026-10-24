"""Home tab: one status card for each part of Meridian, each checked on its own. Qt code, so it sits in ui/.

The cards are the local Meridian server, the tunnel, the services (the LaTeX engine and the slot ports), Zotero, the
AI clients and their tools, and the updates. Each card runs its check when the window first shows, again every five
minutes while Home is showing, and when Check now is pressed. No button starts the first check.

The checks read only, with two exceptions the owner confirms first. The tunnel's Start, Stop and Restart use the
runner controls (ui/scope_controls.py), and Launch Zotero asks in the same confirmation dialog the Zotero panel uses.
The Settings buttons only switch to the Settings tab.

Every check runs on the thread pool through runner_actions.submit, so the window stays responsive, and no check holds
a card. Each card keeps a generation number, so an answer from a superseded check is dropped. A check that has not
finished after STALL_SECONDS stops blocking its card: the card says so, and a new check can start. The stalled check
is not cancelled, and if it finishes, its answer still shows unless a newer check has replaced it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QHideEvent, QShowEvent, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QGridLayout,
)

from meridian_desktop import local_client, zotero_client
from meridian_desktop.home_status import (
    ACTION_CHECK_TOOLS,
    ACTION_LAUNCH_ZOTERO,
    ACTION_RELEASE_PAGE,
    ACTION_SETTINGS,
    CHECKING_HEADLINE,
    CardView,
    LocalFacts,
    RowView,
    ServiceFacts,
    checked_text,
    checking_view,
    failed_view,
    local_view,
    services_view,
    stalled_view,
    tools_view,
    tunnel_view,
    updates_view,
    zotero_view,
)
from meridian_desktop.latex_client import EngineStatus
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.runner_controls import SCOPE_TUNNEL
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.slot_ports import SlotPortResult, probe_slot_ports
from meridian_desktop.tools_status import ToolsStatus
from meridian_desktop.tunnel_status import read_tunnel_status
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions, submit
from meridian_desktop.ui.scope_controls import ScopeControls, tunnel_gate
from meridian_desktop.ui.services_page import probe_engine_status
from meridian_desktop.ui.text import (
    BlockLine,
    block_html,
    enable_widget_copy,
    make_selectable,
    paragraph_tops,
    set_label_text,
)
from meridian_desktop.ui.update_notice import UpdateNoticeBar, open_release_page
from meridian_desktop.ui.zotero_panel import ask_start

# Every card checks again this often while Home is showing (seconds, for the monotonic clock).
CHECK_INTERVAL_SECONDS = 5 * 60
CHECK_INTERVAL_MS = CHECK_INTERVAL_SECONDS * 1000
# A check that has not finished after this many seconds stops blocking its card (see the module docstring).
STALL_SECONDS = 60
INTRO_TEXT = (
    "Each card checks its part of Meridian on its own: when this window opens, and again every five minutes while "
    "Home is showing. The checks only read. Launch Zotero and the tunnel buttons ask before they act."
)
CHECK_NOW_TEXT = "Check now"
TITLE_TEXT = "Home"
OPEN_SETTINGS_TEXT = "Open Settings"
LAUNCH_CANCELLED_TEXT = "Launch cancelled. Nothing was started and no file was written."
LAUNCHING_TEXT = "Launching Zotero..."
LAUNCH_FAILED_TEXT = "Zotero did not launch ({}). Press Check now to see its state."
API_READY_TEXT = "The local API answers now."
API_NOT_READY_TEXT = "The local API is not answering yet. Zotero can take a little while to open. Press Check now in a moment."

# Card colours: a left border per tone. The tone is a property, so the colour follows the card's state. A card that has
# focus (for Ctrl+C) shows it on its other three sides.
CARD_STYLE = """
QFrame[homeCard="true"] { border: 1px solid palette(mid); border-left-width: 4px; border-radius: 6px; }
QFrame[homeCard="true"]:focus {
    border-top-color: palette(highlight); border-right-color: palette(highlight); border-bottom-color: palette(highlight);
}
QFrame[homeCard="true"][tone="ok"] { border-left-color: #2e7d32; }
QFrame[homeCard="true"][tone="attention"] { border-left-color: #b26a00; }
QFrame[homeCard="true"][tone="checking"] { border-left-color: #1565c0; }
QFrame[homeCard="true"][tone="off"] { border-left-color: palette(mid); }
QFrame[homeCard="true"][tone="idle"] { border-left-color: palette(mid); }
"""
# The gaps a card's rows had when each line was a label of its own: between a row and its detail line, between one row
# and the next, and between a row and its button (pixels).
ROW_DETAIL_GAP = 2
ROW_GAP = 6
ROW_BUTTON_GAP = 6


# ----------------------------------------------------------------- the reads (run on the thread pool)

def _local_facts(store: SettingsStore, probe: Callable[[str], str]) -> LocalFacts:
    """The local server's health, at the Local URL, and only on a loopback address. The probe never runs otherwise."""
    url = store.load().settings.local_url
    try:
        local_client.assert_loopback(url)
    except local_client.LocalClientError as exc:
        return LocalFacts(url=url, refusal=str(exc))
    return LocalFacts(url=url, health=probe(url))


def _services_facts(
    store: SettingsStore,
    engine: Callable[[str], EngineStatus],
    ports: Callable[[], Sequence[SlotPortResult]],
) -> ServiceFacts:
    """The LaTeX engine and the slot ports. Each read is guarded alone, so one failure does not hide the other."""
    url = store.load().settings.engine_url
    engine_status: EngineStatus | None = None
    engine_failure = ""
    try:
        engine_status = engine(url)
    except Exception as exc:  # noqa: BLE001 - one failed read must not hide the other
        engine_failure = exc.__class__.__name__
    ports_list: tuple[SlotPortResult, ...] | None = None
    ports_failure = ""
    try:
        ports_list = tuple(ports())
    except Exception as exc:  # noqa: BLE001 - one failed read must not hide the other
        ports_failure = exc.__class__.__name__
    return ServiceFacts(
        engine_url=url,
        engine=engine_status,
        engine_failure=engine_failure,
        ports=ports_list,
        ports_failure=ports_failure,
    )


def _default_engine(url: str) -> EngineStatus:
    """The real LaTeX engine probe. Looked up when called, so a test can replace it."""
    return probe_engine_status(url)


def _default_ports() -> Sequence[SlotPortResult]:
    """The real slot port scan. Looked up when called, so a test can replace it."""
    return probe_slot_ports()


def _launch_zotero(client: Any, exe: str) -> tuple[str, bool]:
    """Start Zotero, then ask its local API once. Runs on a worker thread, and only after the owner says yes."""
    line = client.start_zotero(exe)
    check = client.probe_api()
    return line, check.state == zotero_client.API_ON


# ----------------------------------------------------------------- one card

def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _headline_point_size(font: QFont) -> float:
    return font.pointSizeF() * 1.15 if font.pointSizeF() > 0 else 11.0


def _line_height(text: str, font: QFont) -> int:
    """The height ``text`` takes on lines of its own with no wrapping, as a label of its own showed it."""
    document = QTextDocument()
    document.setDocumentMargin(0)
    document.setDefaultFont(font)
    document.setPlainText(text)
    return math.ceil(document.size().height())


class _Card(QFrame):
    """One status card: a title, a headline, a detail sentence, the time checked, a note, rows, and one action.

    ``work`` runs off the UI thread and returns the card's value. ``render`` turns that value and the time checked
    into a CardView. A card with no ``work`` is drawn by its owner (the Updates card reads a bar, and reads no thread).

    A drag cannot cross from one label to the next, so every line of the card (title, headline, detail, checked line,
    note and rows) is one selectable text block, home-<key>-text. Each line is a paragraph with the font and the gaps it
    had as a label of its own, so one drag from the first line to the last selects them all and Ctrl+C copies them. A
    row's button sits at the block's right edge, level with the row's first line, and the row's text keeps that room
    free. Widgets put under the text (the tunnel's buttons) and the action button are not part of the block.

    Each line also keeps a label of its own under its old name (home-<key>-headline and so on), never shown, for code
    that reads one line. The card has a Copy action of its own too: in its right-click menu, and on Ctrl+C while the card
    has focus. It copies the text the card shows, never a hidden line or a button's text. A redraw keeps a selection
    whose text is still there (set_label_text).
    """

    def __init__(
        self,
        key: str,
        title: str,
        *,
        work: Callable[[], Any] | None,
        render: Callable[[Any, str], CardView] | None,
        on_action: Callable[[str], None],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"home-card-{key}")
        self.setProperty("homeCard", True)
        self.setProperty("tone", "idle")
        self._key = key
        self._work = work
        self._render = render
        self._on_action = on_action
        self._busy = False
        self._generation = 0
        self._owner = object()
        self._action_key = ""
        self._action_busy = False
        self._action_generation = 0
        self._action_owner = object()
        self._action_done: Callable[[Any, str], None] | None = None
        # True once the card shows a check's answer. A new check then keeps that answer on screen until the next one.
        self._has_answer = False
        self._shown_rows: tuple[RowView, ...] = ()
        # Each row's button by row number, and the paragraph of the text block where each row starts.
        self._row_buttons: dict[int, QPushButton] = {}
        self._row_paragraphs: list[int] = []

        self._stall = QTimer(self)
        self._stall.setSingleShot(True)
        self._stall.setInterval(int(STALL_SECONDS * 1000))
        self._stall.timeout.connect(self._on_stalled)
        shared_delivery().delivered.connect(self._on_delivered)
        shared_delivery().delivered.connect(self._on_action_delivered)

        enable_widget_copy(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        # The one text block. A right-click on it opens the card's own menu, whose Copy copies the whole card.
        self._text = make_selectable(QLabel(self))
        self._text.setObjectName(f"home-{key}-text")
        self._text.setTextFormat(Qt.TextFormat.RichText)
        self._text.setWordWrap(True)
        self._text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._text.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._text.installEventFilter(self)
        layout.addWidget(self._text)
        self._title = self._line("title", title)
        self._headline = self._line("headline")
        self._detail = self._line("detail")
        self._checked = self._line("checked")
        self._note = self._line("note")
        self._body = QVBoxLayout()
        layout.addLayout(self._body)
        self._action = QPushButton(self)
        self._action.setObjectName(f"home-{key}-action")
        self._action.hide()
        self._action.clicked.connect(lambda _checked=False: self._on_action(self._action_key))
        layout.addWidget(self._action, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self._draw_text()

    def _line(self, name: str, text: str = "") -> QLabel:
        """One line's own label, under its old name. It is never shown: the text block shows the line."""
        label = _label(self, f"home-{self._key}-{name}", text)
        label.hide()
        return label

    # ------------------------------------------------------------ public

    def add_body(self, widget: QWidget) -> None:
        """Put a widget (the tunnel's control buttons) under the card's text."""
        self._body.addWidget(widget)

    def check(self, *, replace: bool = False) -> None:
        """Run this card's read on the thread pool. A read already running is not doubled, and a stalled one is replaced.

        ``replace`` starts a new read even when one is running. The older answer is then dropped when it arrives, because
        the generation has moved on, so a read that began before the core was found cannot show after the newer one.

        A card that already shows an answer keeps it, and its checked line says Checking until the new answer arrives, so
        a selection in the card stays. A card with no answer yet shows Checking as its headline.
        """
        if self._work is None or (self._busy and not replace):
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._stall.start()
        if self._has_answer:
            set_label_text(self._checked, CHECKING_HEADLINE)
            self._draw_text()
        else:
            self.show_view(checking_view())
        submit(self._owner, generation, self._work)

    def run_action(self, work: Callable[[], Any], done: Callable[[Any, str], None]) -> None:
        """Run one owner-confirmed action on the thread pool, and hand its (value, failure) to ``done`` on the GUI thread."""
        if self._action_busy:
            return
        self._action_busy = True
        self._action_generation += 1
        self._action_done = done
        self.set_note(LAUNCHING_TEXT)
        submit(self._action_owner, self._action_generation, work)

    def set_note(self, text: str) -> None:
        set_label_text(self._note, text)
        self._draw_text()

    def show_view(self, view: CardView) -> None:
        """Draw a CardView. Every field is plain text.

        The text block is set again only when a line changed, and the row buttons are rebuilt only when the rows
        changed, so a redraw that changes nothing keeps a selection, and one that changes a line keeps it when its text
        is still there.
        """
        set_label_text(self._headline, view.headline)
        set_label_text(self._detail, view.detail)
        set_label_text(self._checked, view.checked)
        if self.property("tone") != view.tone:
            self.setProperty("tone", view.tone)
            self.style().unpolish(self)
            self.style().polish(self)
        if view.rows != self._shown_rows:
            self._set_rows(view.rows)
        self._draw_text()
        self._action_key = view.action
        if self._action.text() != view.action_label:
            self._action.setText(view.action_label)
        self._action.setVisible(bool(view.action))

    def _set_rows(self, rows: tuple[RowView, ...]) -> None:
        """Replace the row buttons. An old button is hidden at once, so nothing presses it before it is deleted."""
        for button in self._row_buttons.values():
            button.hide()
            button.deleteLater()
        self._row_buttons = {}
        for index, row in enumerate(rows):
            if not row.action:
                continue
            button = QPushButton(row.action_label, self)
            button.setObjectName(f"home-{self._key}-row-{index}-action")
            button.clicked.connect(lambda _checked=False, key=row.action: self._on_action(key))
            button.resize(button.sizeHint())
            button.show()
            self._row_buttons[index] = button
        self._shown_rows = rows

    def _draw_text(self) -> None:
        """Draw every line the card shows into its one text block, then put each row's button beside its row.

        The gaps are the ones the lines had as labels of their own: the card's spacing between its lines, ROW_DETAIL_GAP
        between a row and its detail, and ROW_GAP between rows. A row with a button keeps the button's width and
        ROW_BUTTON_GAP free at its right, and is at least as tall as its button.
        """
        font = self._text.font()
        spacing = max(self.layout().spacing(), 0)
        lines = [
            BlockLine(text, bold=bold, point_size=size, wrap=wrap)
            for text, bold, size, wrap in (
                (self._title.text(), True, 0.0, False),
                (self._headline.text(), True, _headline_point_size(font), True),
                (self._detail.text(), False, 0.0, True),
                (self._checked.text(), False, 0.0, True),
                (self._note.text(), False, 0.0, True),
            )
            if text
        ]
        lines = [replace(line, space_after=spacing) for line in lines]
        if lines and not self._shown_rows:
            lines[-1] = replace(lines[-1], space_after=0)
        self._row_paragraphs = []
        for index, row in enumerate(self._shown_rows):
            button = self._row_buttons.get(index)
            right = 0
            pad = 0
            if button is not None:
                hint = button.sizeHint()
                right = hint.width() + ROW_BUTTON_GAP
                height = _line_height(row.text, font)
                if row.detail:
                    height += ROW_DETAIL_GAP + _line_height(row.detail, font)
                pad = max(hint.height() - height, 0)
            after = (ROW_GAP if index < len(self._shown_rows) - 1 else 0) + pad
            self._row_paragraphs.append(len(lines))
            if row.detail:
                lines.append(BlockLine(row.text, space_after=ROW_DETAIL_GAP, right_margin=right))
                lines.append(BlockLine(row.detail, space_after=after, right_margin=right))
            else:
                lines.append(BlockLine(row.text, space_after=after, right_margin=right))
        set_label_text(self._text, block_html(lines, font))
        self._place_row_buttons()

    def _place_row_buttons(self) -> None:
        """Put each row's button at the right edge of the text block, level with the first line of its row."""
        if not self._row_buttons:
            return
        tops = paragraph_tops(self._text)
        area = self._text.geometry()
        for index, button in self._row_buttons.items():
            if index >= len(self._row_paragraphs) or self._row_paragraphs[index] >= len(tops):
                continue
            size = button.sizeHint()
            top = area.top() + tops[self._row_paragraphs[index]]
            button.setGeometry(area.right() + 1 - size.width(), top, size.width(), size.height())
            button.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        # The text block moved or changed size, so its rows moved: the row buttons follow them.
        if watched is self._text and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._place_row_buttons()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------ results (GUI thread)

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # A result for another card, or a superseded check, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, payload = item.value
        # Only the newest check's answer is kept. A check that stalled and was then replaced is dropped here, and a
        # stalled check that finishes before any newer one still shows.
        if generation != self._generation:
            return
        self._stall.stop()
        self._busy = False
        self._has_answer = True
        value, failure = payload
        checked = checked_text(datetime.now())
        if failure is not None or self._render is None:
            self.show_view(failed_view(failure or "NoResult", checked))
            return
        self.show_view(self._render(value, checked))

    @Slot(object)
    def _on_action_delivered(self, item: object) -> None:
        if not isinstance(item, Delivered) or item.owner is not self._action_owner:
            return
        generation, (value, failure) = item.value
        if generation != self._action_generation or self._action_done is None:
            return
        done = self._action_done
        self._action_done = None
        self._action_busy = False
        done(value, failure or "")

    @Slot()
    def _on_stalled(self) -> None:
        """The check has taken longer than STALL_SECONDS. The card says so, and a new check can start."""
        if not self._busy:
            return
        self._busy = False
        # The stalled line is not an answer, so the next check shows Checking as its headline again.
        self._has_answer = False
        self.show_view(stalled_view(STALL_SECONDS))


# ----------------------------------------------------------------- the tab

@dataclass(frozen=True)
class HomeDeps:
    """Everything the cards read or do. The window passes the real ones. Tests pass fakes and no socket is opened.

    ``probe`` is the local server's health probe (DataSources.probe). ``engine`` and ``ports`` are the LaTeX engine
    and slot port reads. ``tunnel`` is the runner's status call. ``actions`` holds the tunnel controls and the Zotero
    dialog's settings. ``zotero`` is the Zotero client module, or a fake with the same three calls. ``tools`` is the
    one tools read. ``updates`` is the status bar's update notice, which holds the one startup check.
    """

    store: SettingsStore
    probe: Callable[[str], str] = local_client.probe_health
    engine: Callable[[str], EngineStatus] | None = None
    ports: Callable[[], Sequence[SlotPortResult]] | None = None
    tunnel: Callable[[str], RunnerResult] | None = None
    actions: RunnerActions | None = None
    zotero: Any = zotero_client
    tools: Callable[[], ToolsStatus] | None = None
    updates: UpdateNoticeBar | None = None
    current_version: str = ""


class HomeTab(QWidget):
    """The Home tab: the cards in two columns, a Check now button, and the one-line intro.

    ``settingsRequested`` is emitted when a card's Settings button is pressed. The window connects it to its own
    Settings switch. A signal, not a callback, so the tab never holds the window in memory.
    """

    settingsRequested = Signal()

    def __init__(self, deps: HomeDeps, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("home-tab")
        self._deps = deps
        self._last_run: float | None = None
        self._zotero_status: Any = None
        self.setStyleSheet(CARD_STYLE)

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check_all)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = _label(self, "home-title", TITLE_TEXT)
        title_font = title.font()
        title_font.setBold(True)
        title_font.setPointSizeF(title_font.pointSizeF() * 1.3 if title_font.pointSizeF() > 0 else 14.0)
        title.setFont(title_font)
        header.addWidget(title)
        header.addStretch(1)
        self._check_now = QPushButton(CHECK_NOW_TEXT, self)
        self._check_now.setObjectName("home-check-now")
        self._check_now.clicked.connect(lambda _checked=False: self.check_all())
        header.addWidget(self._check_now)
        layout.addLayout(header)
        layout.addWidget(_label(self, "home-intro", INTRO_TEXT))

        scroll = QScrollArea(self)
        scroll.setObjectName("home-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget(scroll)
        container.setObjectName("home-cards")
        grid = QGridLayout(container)
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self._build_cards(container, grid)
        self._controls_row()
        self.refresh_tunnel_gate()

    # ------------------------------------------------------------ building

    def _build_cards(self, container: QWidget, grid: QGridLayout) -> None:
        deps = self._deps
        store = deps.store
        engine = deps.engine if deps.engine is not None else _default_engine
        ports = deps.ports if deps.ports is not None else _default_ports
        self._local = _Card(
            "local",
            "Local Meridian server",
            work=partial(_local_facts, store, deps.probe),
            render=lambda facts, checked: local_view(facts, checked=checked),
            on_action=self._on_action,
            parent=container,
        )
        self._tunnel = _Card(
            "tunnel",
            "Tunnel",
            work=partial(read_tunnel_status, deps.tunnel) if deps.tunnel is not None else None,
            render=lambda status, checked: tunnel_view(status, checked=checked),
            on_action=self._on_action,
            parent=container,
        )
        self._services = _Card(
            "services",
            "Services",
            work=partial(_services_facts, store, engine, ports),
            render=lambda facts, checked: services_view(facts, checked=checked),
            on_action=self._on_action,
            parent=container,
        )
        self._zotero = _Card(
            "zotero",
            "Zotero",
            work=deps.zotero.status,
            render=self._render_zotero,
            on_action=self._on_action,
            parent=container,
        )
        self._tools = _Card(
            "tools",
            "AI clients and tools",
            work=deps.tools,
            render=lambda status, checked: tools_view(status, checked=checked),
            on_action=self._on_action,
            parent=container,
        )
        self._updates = _Card(
            "updates",
            "Updates",
            work=None,
            render=None,
            on_action=self._on_action,
            parent=container,
        )
        for card, row, column in (
            (self._local, 0, 0),
            (self._tunnel, 0, 1),
            (self._services, 1, 0),
            (self._zotero, 1, 1),
            (self._updates, 2, 0),
            (self._tools, 2, 1),
        ):
            grid.addWidget(card, row, column)
        grid.setRowStretch(3, 1)
        if deps.updates is not None:
            deps.updates.resultChanged.connect(self._refresh_updates)
        self._refresh_updates()

    def _controls_row(self) -> None:
        """The tunnel's Start, Stop and Restart (ScopeControls, which asks first) and an Open Settings button when they are off."""
        deps = self._deps
        if deps.actions is None:
            return
        self._tunnel_controls = ScopeControls(SCOPE_TUNNEL, deps.actions, tunnel_gate, prefix="tunnel", parent=self._tunnel)
        self._tunnel_controls.actionFinished.connect(self._tunnel.check)
        self._tunnel.add_body(self._tunnel_controls)
        self._tunnel_settings = QPushButton(OPEN_SETTINGS_TEXT, self._tunnel)
        self._tunnel_settings.setObjectName("home-tunnel-settings")
        self._tunnel_settings.clicked.connect(lambda _checked=False: self._open_settings())
        self._tunnel.add_body(self._tunnel_settings)
        # The launch-time core search can finish after the boot checks, so the core-dependent cards read again then.
        deps.actions.coreChanged.connect(self._on_core_changed)

    def _on_core_changed(self) -> None:
        """The core search finished, or the core setup was saved: the tunnel buttons and the Tunnel and Tools cards read again.

        The boot checks can run before the launch-time search finds the core, so their first answers can say the core is not
        set. A search that is still running is skipped here, because its finish comes back through this slot.
        """
        actions = self._deps.actions
        if actions is None or actions.locating:
            return
        self.refresh_tunnel_gate()
        self._tunnel.check(replace=True)
        self._tools.check(replace=True)

    def refresh_tunnel_gate(self) -> None:
        """Read the saved core setup again. The tunnel buttons are on only when the gate allows, and Open Settings shows when they are off."""
        deps = self._deps
        if deps.actions is None:
            return
        self._tunnel_controls.refresh_enabled()
        enabled, _note = tunnel_gate(deps.actions.core())
        self._tunnel_settings.setVisible(not enabled)

    # ------------------------------------------------------------ checks

    def check_all(self) -> None:
        """Run every card's check now. The Updates card reads the one startup check, so it makes no request."""
        self._last_run = time.monotonic()
        for card in (self._local, self._tunnel, self._services, self._zotero, self._tools):
            card.check()
        self._refresh_updates()

    def _refresh_updates(self) -> None:
        deps = self._deps
        bar = deps.updates
        if bar is None:
            self._updates.show_view(updates_view(deps.current_version, None, enabled=False, checking=False))
            return
        view = updates_view(deps.current_version, bar.result, enabled=bar.enabled, checking=bar.checking)
        self._updates.show_view(view)

    def _render_zotero(self, status: Any, checked: str) -> CardView:
        self._zotero_status = status
        return zotero_view(status, checked=checked)

    # ------------------------------------------------------------ actions

    def _on_action(self, key: str) -> None:
        if key == ACTION_CHECK_TOOLS:
            self._tools.check()
        elif key == ACTION_SETTINGS:
            self._open_settings()
        elif key == ACTION_LAUNCH_ZOTERO:
            self._launch_zotero()
        elif key == ACTION_RELEASE_PAGE:
            self._open_release_page()

    def _open_settings(self) -> None:
        self.settingsRequested.emit()

    def _open_release_page(self) -> None:
        bar = self._deps.updates
        url = bar.release_page_url if bar is not None else ""
        if url:
            open_release_page(url)

    def _launch_zotero(self) -> None:
        """Ask first, then start Zotero on the thread pool. Nothing starts without the owner's yes."""
        status = self._zotero_status
        exe = getattr(status, "exe", None)
        if not exe or getattr(status, "process_running", None) is True:
            return
        command = zotero_client.command_line(exe)
        folder = str(Path(exe).parent)
        if not ask_start(self, command, folder):
            self._zotero.set_note(LAUNCH_CANCELLED_TEXT)
            return
        client = self._deps.zotero
        self._zotero.run_action(partial(_launch_zotero, client, exe), self._zotero_launched)

    def _zotero_launched(self, value: Any, failure: str) -> None:
        if failure or not isinstance(value, tuple):
            self._zotero.set_note(LAUNCH_FAILED_TEXT.format(failure or "no result"))
        else:
            line, ready = value
            self._zotero.set_note(f"{line}\n{API_READY_TEXT if ready else API_NOT_READY_TEXT}")
        self._zotero.check()

    # ------------------------------------------------------------ events

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._timer.start()
        self.refresh_tunnel_gate()
        stale = self._last_run is None or time.monotonic() - self._last_run >= CHECK_INTERVAL_SECONDS
        if stale:
            self.check_all()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        self._timer.stop()


__all__ = [
    "CHECK_INTERVAL_MS",
    "CHECK_INTERVAL_SECONDS",
    "CHECK_NOW_TEXT",
    "STALL_SECONDS",
    "HomeDeps",
    "HomeTab",
]
