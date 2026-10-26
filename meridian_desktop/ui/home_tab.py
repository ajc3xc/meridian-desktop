"""Home tab: one status card for each part of Meridian, each checked on its own. Qt code, so it sits in ui/.

The cards are the local Meridian server, the tunnel, the services (the LaTeX engine and the slot ports), Zotero, the
AI clients and their tools, the updates, and the Overleaf (LaTeX) card. Each card runs its check when the window first shows, again every five
minutes while Home is showing, and when Check now is pressed. No button starts the first check. Each later show reads
the quick cards (local, tunnel, services, Zotero) again, after SHOW_DEBOUNCE_MS. A runner action or a start check
(RunnerActions.runnerStateChanged), from Home or Settings, reads the Local, Tunnel and Tools cards again, and a client
setup change (RunnerActions.clientSetupChanged) reads the Tools card again: at once while Home shows, or on its next
show. A check asked while a card's read runs waits for that read and then runs.

The checks read only, with two exceptions the owner confirms first. The tunnel's and the local server's Start, Stop and
Restart use the runner controls (ui/scope_controls.py), and Launch Zotero asks in the same confirmation dialog the Zotero
panel uses. The Settings buttons only switch to the Settings tab.

The Overleaf (LaTeX) card reads only the LaTeX engine's port: when Home is shown, and on Check now. It asks the engine
about the Overleaf session only on a press of its Check button, once per press, and never on the timer or on show. Log in
asks the engine to open a login window. Start engine asks first (No is the default), then starts the engine's starter.
Once per app launch, run_launch_checks (called by app.py after the window shows) does two things when the owner's
switches allow it: it starts the engine when the engine is down and the owner has chosen its folder, and it checks the
saved session once when the engine is running. Its two switches and Choose engine folder sit on this card. Nothing in
this card stops a process.

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

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QHideEvent, QShowEvent, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QGridLayout,
)

from meridian_desktop import local_client, overleaf_client, overleaf_engine, runner_client, zotero_client
from meridian_desktop.client_setup_runner import COMMAND_TIMEOUT_SECONDS
from meridian_desktop.home_status import (
    ACTION_CHECK_TOOLS,
    ACTION_LAUNCH_ZOTERO,
    ACTION_RELEASE_PAGE,
    ACTION_SETTINGS,
    AUTO_CHECK_LABEL,
    AUTO_CHECK_TOOLTIP,
    AUTO_START_LABEL,
    AUTO_START_TOOLTIP,
    CHECKING_HEADLINE,
    CHOOSE_FOLDER_TEXT,
    CHOOSE_FOLDER_TOOLTIP,
    ENGINE_FOLDER_DIALOG_TITLE,
    ENGINE_FOLDER_NOT_SET_TEXT,
    ENGINE_FOLDER_SAVED_TEXT,
    LAUNCH_STARTING_TEXT,
    LOGIN_FAILED_TEXT,
    NO_STARTER_CHOSEN_TEXT,
    OPTION_NOT_SAVED_TEXT,
    OVERLEAF_ENGINE_DOWN,
    OVERLEAF_ENGINE_FAILED,
    OVERLEAF_ENGINE_RUNNING,
    OVERLEAF_ENGINE_UNCHECKED,
    OVERLEAF_TITLE,
    SESSION_CHECK_FAILED_TEMPLATE,
    START_CANCELLED_TEXT,
    START_FAILED_TEMPLATE,
    START_QUESTION,
    START_TITLE,
    START_WORKING_TEXT,
    TIME_FORMAT,
    CardView,
    LocalFacts,
    OverleafFacts,
    RowView,
    ServiceFacts,
    checked_text,
    checking_view,
    failed_view,
    launch_start_text,
    local_view,
    overleaf_login_text,
    overleaf_start_text,
    overleaf_view,
    services_view,
    stalled_view,
    tools_view,
    tunnel_view,
    updates_view,
    zotero_view,
)
from meridian_desktop.latex_client import EngineStatus, address_text, port_text
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.runner_controls import SCOPE_LOCAL, SCOPE_TUNNEL
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.slot_ports import SlotPortResult, probe_slot_ports
from meridian_desktop.tools_status import ToolsStatus
from meridian_desktop.tunnel_status import read_tunnel_status
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions, submit
from meridian_desktop.ui.scope_controls import ScopeControls, local_gate, tunnel_gate
from meridian_desktop.ui.services_page import probe_engine_status
from meridian_desktop.ui.text import (
    BlockLine,
    block_html,
    enable_widget_copy,
    hidden_on_purpose,
    make_selectable,
    paragraph_tops,
    set_label_text,
    text_bottom,
)
from meridian_desktop.ui.update_notice import UpdateNoticeBar, open_release_page
from meridian_desktop.ui.zotero_panel import ask_start

# Every card checks again this often while Home is showing (seconds, for the monotonic clock).
CHECK_INTERVAL_SECONDS = 5 * 60
CHECK_INTERVAL_MS = CHECK_INTERVAL_SECONDS * 1000
# A check that has not finished after this many seconds stops blocking its card (see the module docstring). The Tools
# read is the longest: the core's client setup plan, then the local server probe (which can take up to twice its
# timeout, see local_client._get), then the tunnel status call, one after the other. The stall comes only after all of
# them could have run to their timeouts, with a margin, so a slow read still within its timeouts never shows Still checking.
STALL_MARGIN_SECONDS = 15
STALL_SECONDS = int(
    COMMAND_TIMEOUT_SECONDS + 2 * local_client.DEFAULT_TIMEOUT + runner_client.DEFAULT_TIMEOUT + STALL_MARGIN_SECONDS
)
# Showing Home again reads the quick cards after this pause, so a few quick tab switches run one read, not several.
SHOW_DEBOUNCE_MS = 300
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
# Start engine waits this long for the engine's port after the starter runs, and looks again this often.
START_WAIT_SECONDS = 8.0
START_POLL_SECONDS = 0.25
OVERLEAF_CHECK_TOOLTIP = "Sends your saved session to Overleaf once, to see if you are signed in."
OVERLEAF_LOGIN_TOOLTIP = "Opens a login window in Chrome. Sign in there, then press Check."
OVERLEAF_START_TOOLTIP = "Starts the LaTeX engine in the background. It asks first."

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


@dataclass(frozen=True)
class OverleafProbe:
    """What the Overleaf card learns from the engine's port: its address, whether it accepts a connection, and where its
    starter is (None when that is not known). No Overleaf request is made for this."""

    url: str
    listening: bool
    folder: Path | None


def _overleaf_probe(store: SettingsStore, deps: OverleafDeps) -> OverleafProbe:
    """The engine's port and the folder the owner chose. Runs on the thread pool. It never sends a request to Overleaf."""
    settings = store.load().settings
    url = settings.engine_url
    return OverleafProbe(url=url, listening=bool(deps.listening(url)), folder=deps.folder(settings))


def _overleaf_session_read(store: SettingsStore, deps: OverleafDeps) -> Any:
    """Ask the engine once about the saved Overleaf session. Runs on the thread pool, and only on a Check press."""
    return deps.session(store.load().settings.engine_url)


def _overleaf_login_read(store: SettingsStore, deps: OverleafDeps) -> Any:
    """Ask the engine to open its login window. Runs on the thread pool, and only on a Log in press."""
    return deps.login(store.load().settings.engine_url)


def _overleaf_start_read(store: SettingsStore, deps: OverleafDeps, folder: Path) -> tuple[bool, str]:
    """Start the engine's starter, then wait up to deps.wait_seconds for its port. Returns (running, reason). The reason is
    fixed text, set only when the starter did not run. Runs on the thread pool, and only after the owner says yes."""
    url = store.load().settings.engine_url
    started = deps.start(folder)
    if not started.started:
        return False, started.reason
    deadline = time.monotonic() + deps.wait_seconds
    while True:
        if deps.listening(url):
            return True, ""
        if time.monotonic() >= deadline:
            return False, ""
        time.sleep(START_POLL_SECONDS)


def _default_overleaf_deps() -> OverleafDeps:
    """The real LaTeX engine calls. Looked up when called, so a test can replace this function."""
    return OverleafDeps(
        listening=lambda url: overleaf_engine.engine_listening(url),
        folder=lambda settings: overleaf_engine.engine_folder(settings),
        start=lambda folder: overleaf_engine.start_engine(folder),
        session=lambda url: overleaf_client.session_status(url),
        login=lambda url: overleaf_client.start_login(url),
    )


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


class _ButtonFlow(QLayout):
    """Buttons in rows that wrap: left to right, and the ones that do not fit go on the next row.

    When the width allows, the buttons sit in one row, as ScopeControls' own row showed them. A narrower card puts them on
    two rows or more instead of growing wider than its column, so the row never makes Home scroll sideways. Each button
    keeps its own size, so its whole text shows. A button hidden on purpose (the fix button while the gate is on) takes no
    room. The sizes are read from the buttons themselves, so they are right before the card is first shown too.
    """

    def __init__(self, spacing: int) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt naming
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt naming
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt naming
        if not 0 <= index < len(self._items):
            return None
        item = self._items.pop(index)
        self.invalidate()
        return item

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt naming
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def isEmpty(self) -> bool:  # noqa: N802 - Qt naming
        return not self._shown()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """Every button on one row."""
        sizes = [widget.sizeHint() for widget in self._shown()]
        width = sum(size.width() for size in sizes) + self.spacing() * max(len(sizes) - 1, 0)
        return QSize(width, max((size.height() for size in sizes), default=0))

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        """As wide as the widest button, with each button on a row of its own."""
        widest = self.widest()
        return QSize(widest, self.heightForWidth(widest))

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def widest(self) -> int:
        return max((widget.sizeHint().width() for widget in self._shown()), default=0)

    def _shown(self) -> list[QWidget]:
        widgets = (item.widget() for item in self._items)
        return [widget for widget in widgets if widget is not None and not hidden_on_purpose(widget)]

    def _arrange(self, rect: QRect, *, place: bool) -> int:
        """Lay the buttons out in ``rect``'s width, from its top left, and return the height they take."""
        spacing = max(self.spacing(), 0)
        x, y, row_height = rect.x(), rect.y(), 0
        for widget in self._shown():
            size = widget.sizeHint()
            if x > rect.x() and x + size.width() > rect.x() + rect.width():
                x, y, row_height = rect.x(), y + row_height + spacing, 0
            if place:
                widget.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + spacing
            row_height = max(row_height, size.height())
        return y + row_height - rect.y()


class _CardControls(ScopeControls):
    """A runner's Start, Stop and Restart on a Home card (ScopeControls), whose note and result lines are the card's.

    ScopeControls shows its gate note and its last result in labels of its own, under its buttons. A drag cannot cross
    from the card's text block into another label, so here those two labels are never shown. They keep their text under
    their names (tunnel-control-result, home-local-control-note and so on) for code that reads it, and the card draws
    the same lines into its one text block, under the buttons (_Card.add_controls). ``linesChanged`` is emitted each
    time either line may have changed. The buttons, the questions they ask and the calls they run are ScopeControls'
    own, unchanged.

    ScopeControls keeps its buttons on one row, which would make the card as wide as all of them. Here the same buttons,
    in the same order, sit in a row that wraps (_ButtonFlow), so the card keeps its share of Home's width.
    """

    linesChanged = Signal()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        outer = self.layout()
        row = outer.itemAt(0).layout()
        self._flow = _ButtonFlow(max(row.spacing(), 0))
        outer.insertLayout(0, self._flow)
        for button in (self._start, self._stop, self._restart, self._fix):
            row.removeWidget(button)
            self._flow.addWidget(button)
        outer.removeItem(row)
        row.deleteLater()

    def height_for(self, width: int) -> int:
        """How tall the buttons are when they wrap at ``width``."""
        return self._flow.heightForWidth(width)

    def widest(self) -> int:
        """The width of the widest button shown, the least width at which every button shows whole."""
        return self._flow.widest()

    def lines(self) -> tuple[str, ...]:
        """The lines ScopeControls shows under its buttons: the gate note, then the last result, each when it is set."""
        return tuple(label.text() for label in (self._note, self._result) if label.text())

    def refresh_enabled(self) -> None:
        super().refresh_enabled()
        self._note.hide()
        self.linesChanged.emit()

    def _show(self, text: str) -> None:
        super()._show(text)
        self._result.hide()
        self.linesChanged.emit()


class _Card(QFrame):
    """One status card: a title, a headline, a detail sentence, the time checked, a note, rows, and one action.

    ``work`` runs off the UI thread and returns the card's value. ``render`` turns that value and the time checked
    into a CardView. A card with no ``work`` is drawn by its owner (the Updates card reads a bar, and reads no thread).

    A drag cannot cross from one label to the next, so every line of the card (title, headline, detail, checked line,
    note, rows, and the note and result lines under a runner's buttons) is one selectable text block, home-<key>-text.
    Each line is a paragraph with the font and the gaps it had as a label of its own, so one drag from the first line to
    the last selects them all and Ctrl+C copies them. A row's button sits at the block's right edge, level with the
    row's first line, and the row's text keeps that room free. A runner's Start, Stop and Restart (add_controls) sit in
    a gap of the block under the card's own lines, where they sat before, and their note and result lines follow them
    in the block. The buttons wrap onto more rows when the block is narrower than their row, and the gap follows the
    rows they take, so they never make the card wider than its column. The card is only ever as wide as its widest
    button needs. Widgets put under the text (the tunnel's Open Settings) and the action button are not part of it.

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
        # A check asked while a read runs. It runs once that read has answered.
        self._queued = False
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
        # A runner's buttons (add_controls), and the paragraph of the text block where their first line starts (-1: none).
        self._controls: _CardControls | None = None
        self._control_paragraph = -1
        # The height of the gap the text block keeps for a runner's buttons, and their widest button, at the last draw.
        self._controls_height = 0
        self._controls_widest = 0

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
        """Put a widget (the tunnel's Open Settings) under the card's text."""
        self._body.addWidget(widget)

    def add_controls(self, controls: _CardControls) -> None:
        """Put a runner's Start, Stop and Restart under the card's own lines, and their note and result lines under the
        buttons, in the card's one text block. The buttons are the card's child, placed over a gap in the block."""
        if controls.parentWidget() is not self:
            controls.setParent(self)
            controls.show()
        self._controls = controls
        controls.linesChanged.connect(self._draw_text)
        self._draw_text()

    @property
    def busy(self) -> bool:
        """True while this card's read runs and has not stalled."""
        return self._busy

    def check(self, *, replace: bool = False) -> None:
        """Run this card's read on the thread pool. A read already running is not doubled, and a stalled one is replaced.

        A check asked while a read runs is queued, not dropped: it runs once that read has answered. ``replace`` starts a
        new read even when one is running. The older answer is then dropped when it arrives, because the generation has
        moved on, so a read that began before the core was found, or before a start or stop, cannot show after the newer one.

        A card that already shows an answer keeps it, and its checked line says Checking until the new answer arrives, so
        a selection in the card stays. A card with no answer yet shows Checking as its headline.
        """
        if self._work is None:
            return
        if self._busy and not replace:
            self._queued = True
            return
        self._queued = False
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
        ROW_BUTTON_GAP free at its right, and is at least as tall as its button. A runner's buttons get the card's
        spacing and the height their rows take at the block's width under the card's own lines, and their note and
        result lines follow with the spacing they had in the runner's own layout. The block has no minimum width of its
        own: the buttons wrap instead (_ButtonFlow), and the card keeps room for the widest one (minimumSizeHint).
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
        controls = self._controls
        self._control_paragraph = -1
        if controls is not None:
            # The gap for the buttons, under the card's own lines, then the lines the buttons' own layout showed under them.
            # The gap is as tall as the rows the buttons wrap onto at the block's width. A button just shown or hidden
            # counts, because the rows are read from the buttons themselves.
            control_lines = controls.lines()
            inner = max(controls.layout().spacing(), 0)
            self._controls_height = controls.height_for(self._text.width())
            gap = spacing + self._controls_height + (inner if control_lines else 0)
            lines[-1] = replace(lines[-1], space_after=lines[-1].space_after + gap)
            if control_lines:
                self._control_paragraph = len(lines)
            for number, text in enumerate(control_lines):
                lines.append(BlockLine(text, space_after=inner if number < len(control_lines) - 1 else 0))
        # A label leaves out its last paragraph's bottom margin, so the room under the last line (for a row's button or
        # a runner's buttons) is the block's bottom contents margin instead.
        tail = math.ceil(lines[-1].space_after)
        lines[-1] = replace(lines[-1], space_after=0)
        if self._text.contentsMargins().bottom() != tail:
            self._text.setContentsMargins(0, 0, 0, tail)
        set_label_text(self._text, block_html(lines, font))
        if controls is not None and controls.widest() != self._controls_widest:
            # The widest button shown changed (the fix button came or went), so the card's least width did too.
            self._controls_widest = controls.widest()
            self.updateGeometry()
        self._place_buttons()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """At least as wide as a runner's widest button and the room the card keeps round its text block.

        The buttons wrap, so they never ask for more than that, and every button still shows whole on the card.
        """
        hint = super().minimumSizeHint()
        controls = self._controls
        if controls is None:
            return hint
        frame = self.contentsMargins()
        inner = self.layout().contentsMargins()
        room = frame.left() + frame.right() + inner.left() + inner.right()
        return hint.expandedTo(QSize(controls.widest() + room, 0))

    def _place_buttons(self) -> None:
        """Put each row's button at the right edge of the text block, level with the first line of its row, and a
        runner's buttons across the block, in the gap above their note and result lines (or at the block's foot)."""
        if not self._row_buttons and self._controls is None:
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
        controls = self._controls
        if controls is not None:
            # The gap _draw_text kept for the buttons' rows.
            height = self._controls_height
            if 0 <= self._control_paragraph < len(tops):
                top = tops[self._control_paragraph] - max(controls.layout().spacing(), 0) - height
            else:
                top = text_bottom(self._text) + self._text.contentsMargins().bottom() - height
            controls.setGeometry(area.left(), area.top() + top, area.width(), height)
            # A button shown or hidden at the same size moves the others without a resize, so the rows are laid out now.
            controls.layout().activate()
            controls.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        # The text block moved or changed size, so its rows moved: the row buttons and a runner's buttons follow them.
        # At a new width a runner's buttons can wrap onto another number of rows, and then the gap for them is drawn again.
        if watched is self._text and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            controls = self._controls
            if controls is not None and controls.height_for(self._text.width()) != self._controls_height:
                self._draw_text()
            else:
                self._place_buttons()
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
        else:
            self.show_view(self._render(value, checked))
        if self._queued:
            # A check was asked while this read ran, so it runs now and reads after the answer it waited for.
            self.check()

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

def _choose_engine_folder(parent: QWidget | None, start_dir: str) -> str:
    """Ask for the LaTeX engine's folder. Return its path, or "" when the dialog is cancelled. Nothing is started here."""
    return QFileDialog.getExistingDirectory(parent, ENGINE_FOLDER_DIALOG_TITLE, start_dir)


@dataclass(frozen=True)
class OverleafDeps:
    """The LaTeX engine calls the Overleaf card makes. The window passes the real ones, and tests pass fakes.

    ``listening(url)`` is whether the engine's port accepts a connection (overleaf_engine.engine_listening). ``folder(settings)``
    is the folder the owner chose, or None (overleaf_engine.engine_folder). ``start(folder)`` runs the starter
    (overleaf_engine.start_engine). ``session(url)`` asks the engine once about the Overleaf session, and ``login(url)``
    opens its login window (overleaf_client.session_status and start_login). ``confirm`` asks the owner yes or no before
    Start engine (ask_yes_no, where No is the default and Escape is No). ``wait_seconds`` is how long Start engine waits
    for the port. ``choose_folder(parent, start_dir)`` opens the folder picker and returns the folder, or "" when cancelled.
    """

    listening: Callable[[str], bool]
    folder: Callable[[Settings], Path | None]
    start: Callable[[Path], Any]
    session: Callable[[str], Any]
    login: Callable[[str], Any]
    confirm: Callable[[QWidget, str, str], bool] = ask_yes_no
    wait_seconds: float = START_WAIT_SECONDS
    choose_folder: Callable[[QWidget | None, str], str] = _choose_engine_folder


@dataclass(frozen=True)
class HomeDeps:
    """Everything the cards read or do. The window passes the real ones. Tests pass fakes and no socket is opened.

    ``probe`` is the local server's health probe (DataSources.probe). ``engine`` and ``ports`` are the LaTeX engine
    and slot port reads. ``tunnel`` is the runner's status call. ``actions`` holds the tunnel controls and the Zotero
    dialog's settings. ``zotero`` is the Zotero client module, or a fake with the same three calls. ``tools`` is the
    one tools read. ``updates`` is the status bar's update notice, which holds the one startup check. ``overleaf`` holds
    the Overleaf card's LaTeX engine calls (None: the real ones).
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
    overleaf: OverleafDeps | None = None


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
        # Each later show reads the quick cards again once this short pause has passed (SHOW_DEBOUNCE_MS).
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(SHOW_DEBOUNCE_MS)
        self._show_timer.timeout.connect(self._recheck_on_show)
        # Cards a runner action or a client setup change touched while Home was hidden. They read again on the next show.
        self._stale: set[_Card] = set()
        # The Overleaf (LaTeX) card. Its jobs deliver to the shared object under this owner, and one job of each kind runs at
        # a time (the kind is the job's generation). Nothing is read from the engine until the owner presses a button.
        self._overleaf_deps = deps.overleaf if deps.overleaf is not None else _default_overleaf_deps()
        self._overleaf_owner = object()
        self._overleaf_engine = OVERLEAF_ENGINE_UNCHECKED
        self._overleaf_url = ""
        self._overleaf_folder: Path | None = None
        self._overleaf_session: overleaf_client.SessionStatus | None = None
        self._overleaf_checked = ""
        self._overleaf_busy: set[str] = set()
        self._overleaf_done: dict[str, Callable[[Any, str], None]] = {}
        # The launch-time checks run once per app launch (run_launch_checks). This flag is set on that one run.
        self._launch_checks_ran = False
        shared_delivery().delivered.connect(self._on_overleaf_delivered)

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
        self._check_now.clicked.connect(lambda _checked=False: self._check_now_pressed())
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
        self._overleaf = self._build_overleaf_card(container)
        for card, row, column in (
            (self._local, 0, 0),
            (self._tunnel, 0, 1),
            (self._services, 1, 0),
            (self._zotero, 1, 1),
            (self._updates, 2, 0),
            (self._tools, 2, 1),
            (self._overleaf, 3, 0),
        ):
            grid.addWidget(card, row, column)
        grid.setRowStretch(4, 1)
        if deps.updates is not None:
            deps.updates.resultChanged.connect(self._refresh_updates)
        self._refresh_updates()
        self._refresh_overleaf()

    def _build_overleaf_card(self, container: QWidget) -> _Card:
        """The Overleaf (LaTeX) card: its status lines, then Check, Log in, Start engine and Choose engine folder in one row
        under them, then the two switches.

        The buttons are made here, and Start engine is hidden until it is offered, so the widget count does not depend on the
        engine's state or on whether its folder is known. The switches start checked, the defaults, and showing Home sets them
        to the saved values. Nothing is read here.
        """
        card = _Card("overleaf", OVERLEAF_TITLE, work=None, render=None, on_action=self._on_action, parent=container)
        row = QWidget(card)
        row.setObjectName("home-overleaf-buttons")
        flow = _ButtonFlow(ROW_GAP)
        row.setLayout(flow)
        self._overleaf_check_button = self._overleaf_button(
            row, "home-overleaf-check", "Check", OVERLEAF_CHECK_TOOLTIP, self._on_overleaf_check
        )
        self._overleaf_login_button = self._overleaf_button(
            row, "home-overleaf-login", "Log in", OVERLEAF_LOGIN_TOOLTIP, self._on_overleaf_login
        )
        self._overleaf_start_button = self._overleaf_button(
            row, "home-overleaf-start-engine", "Start engine", OVERLEAF_START_TOOLTIP, self._on_overleaf_start
        )
        self._overleaf_choose_button = self._overleaf_button(
            row, "home-overleaf-choose-folder", CHOOSE_FOLDER_TEXT, CHOOSE_FOLDER_TOOLTIP, self._on_overleaf_choose_folder
        )
        for button in (
            self._overleaf_check_button,
            self._overleaf_login_button,
            self._overleaf_start_button,
            self._overleaf_choose_button,
        ):
            flow.addWidget(button)
        self._overleaf_start_button.setVisible(False)
        card.add_body(row)
        card.add_body(self._build_overleaf_switches(card))
        return card

    def _build_overleaf_switches(self, card: QWidget) -> QWidget:
        """The two Overleaf card switches: start the engine when Meridian opens, and check the session when it opens."""
        box = QWidget(card)
        box.setObjectName("home-overleaf-switches")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        self._overleaf_auto_start_box = self._overleaf_switch(
            box, "home-overleaf-auto-start", AUTO_START_LABEL, AUTO_START_TOOLTIP, "engine_auto_start"
        )
        self._overleaf_auto_check_box = self._overleaf_switch(
            box, "home-overleaf-auto-check", AUTO_CHECK_LABEL, AUTO_CHECK_TOOLTIP, "overleaf_auto_check"
        )
        layout.addWidget(self._overleaf_auto_start_box)
        layout.addWidget(self._overleaf_auto_check_box)
        return box

    def _overleaf_switch(self, parent: QWidget, name: str, text: str, tooltip: str, key: str) -> QCheckBox:
        box = QCheckBox(text, parent)
        box.setObjectName(name)
        box.setToolTip(tooltip)
        # Checked is the default. The signal is connected after that, so setting the default writes nothing.
        box.setChecked(True)
        box.toggled.connect(lambda checked, key=key: self._on_overleaf_switch(key, checked))
        return box

    @staticmethod
    def _overleaf_button(parent: QWidget, name: str, text: str, tooltip: str, on_click: Callable[[], None]) -> QPushButton:
        button = QPushButton(text, parent)
        button.setObjectName(name)
        button.setToolTip(tooltip)
        button.clicked.connect(lambda _checked=False: on_click())
        return button

    def _controls_row(self) -> None:
        """The tunnel's and the local server's Start, Stop and Restart (ScopeControls, which asks first), and an Open
        Settings button on the Tunnel card when its buttons are off."""
        deps = self._deps
        if deps.actions is None:
            return
        # Their note and result lines are lines of the card's own text block (_CardControls), so one drag copies them too.
        self._tunnel_controls = _CardControls(SCOPE_TUNNEL, deps.actions, tunnel_gate, prefix="tunnel", parent=self._tunnel)
        self._tunnel.add_controls(self._tunnel_controls)
        self._tunnel_settings = QPushButton(OPEN_SETTINGS_TEXT, self._tunnel)
        self._tunnel_settings.setObjectName("home-tunnel-settings")
        self._tunnel_settings.clicked.connect(lambda _checked=False: self._open_settings())
        self._tunnel.add_body(self._tunnel_settings)
        # The same local controls Settings has, with the same confirmation. Settings keeps its own.
        self._local_controls = _CardControls(SCOPE_LOCAL, deps.actions, local_gate, prefix="home-local", parent=self._local)
        self._local.add_controls(self._local_controls)
        # The launch-time core search can finish after the boot checks, so the core-dependent cards read again then.
        deps.actions.coreChanged.connect(self._on_core_changed)
        # A Start, Stop or Restart here or in Settings, each check after a start, and a client setup change. Home only
        # reads on these. It never emits coreChanged back, so nothing loops.
        deps.actions.runnerStateChanged.connect(self._on_runner_state_changed)
        deps.actions.clientSetupChanged.connect(self._on_client_setup_changed)

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

    @Slot()
    def _on_runner_state_changed(self) -> None:
        """A runner action or a start check finished, here or in Settings: Local, Tunnel and Tools read again."""
        self._mark_changed((self._local, self._tunnel, self._tools))

    @Slot()
    def _on_client_setup_changed(self) -> None:
        """A client setup Apply or Remove finished in Settings: Tools reads again."""
        self._mark_changed((self._tools,))

    def _mark_changed(self, cards: Sequence[_Card]) -> None:
        """Read these cards again, replacing any read that began before the change. While Home is hidden, they wait for
        the next show, which reads them the same way."""
        self._stale.update(cards)
        if self.isVisible():
            self._read_stale()

    def _read_stale(self) -> None:
        stale, self._stale = self._stale, set()
        for card in self._cards():
            if card in stale:
                card.check(replace=True)

    def _cards(self) -> tuple[_Card, ...]:
        """The cards that run a read, in their order on the tab."""
        return (self._local, self._tunnel, self._services, self._zotero, self._tools)

    def refresh_tunnel_gate(self) -> None:
        """Read the saved core setup again. The tunnel and local buttons are on only when their gate allows, and Open
        Settings shows when the tunnel buttons are off."""
        deps = self._deps
        if deps.actions is None:
            return
        self._tunnel_controls.refresh_enabled()
        self._local_controls.refresh_enabled()
        enabled, _note = tunnel_gate(deps.actions.core())
        self._tunnel_settings.setVisible(not enabled)

    # ------------------------------------------------------------ checks

    def check_all(self) -> None:
        """Run every card's check now. The Updates card reads the one startup check, so it makes no request.

        A card a change touched replaces its running read. Any other card that is still reading queues this check.
        """
        self._last_run = time.monotonic()
        stale, self._stale = self._stale, set()
        for card in self._cards():
            card.check(replace=card in stale)
        self._refresh_updates()

    @Slot()
    def _recheck_on_show(self) -> None:
        """Home was shown again within five minutes. The quick cards read again, and Tools only after a change.

        A quick card that is still reading keeps that read, because it began moments ago. A card a change touched
        replaces its read.
        """
        if not self.isVisible():
            return
        stale, self._stale = self._stale, set()
        for card in (self._local, self._tunnel, self._services, self._zotero):
            if card in stale:
                card.check(replace=True)
            elif not card.busy:
                card.check()
        if self._tools in stale:
            self._tools.check(replace=True)

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

    def _check_now_pressed(self) -> None:
        """Check now runs every card's check, and looks at the LaTeX engine's port again. It sends no Overleaf request."""
        self.check_all()
        self._check_overleaf_engine()

    # ------------------------------------------------------------ the Overleaf (LaTeX) card

    def _run_overleaf(self, kind: str, work: Callable[[], Any], done: Callable[[Any, str], None]) -> bool:
        """Run one Overleaf job on the thread pool. One job of each kind runs at a time; a second one is not started.

        ``done`` receives (value, failure) on the GUI thread, where failure is a class name or "".
        """
        if kind in self._overleaf_busy:
            return False
        self._overleaf_busy.add(kind)
        self._overleaf_done[kind] = done
        submit(self._overleaf_owner, kind, work)
        self._refresh_overleaf()
        return True

    @Slot(object)
    def _on_overleaf_delivered(self, item: object) -> None:
        # Only this card's own jobs are kept. The kind names the job, so each answer reaches its own handler.
        if not isinstance(item, Delivered) or item.owner is not self._overleaf_owner:
            return
        kind, (value, failure) = item.value
        self._overleaf_busy.discard(kind)
        done = self._overleaf_done.pop(kind, None)
        if done is not None:
            done(value, failure or "")
        self._refresh_overleaf()

    def _refresh_overleaf(self) -> None:
        """Draw the card's status from what it holds, and show or enable its buttons to match. A launch-time check reads
        as Checking while it runs, and Start engine is held while a start runs, by hand or at launch."""
        busy = self._overleaf_busy
        launching = "launch" in busy or "launch_start" in busy
        facts = OverleafFacts(
            engine=self._overleaf_engine,
            session=self._overleaf_session,
            checked=self._overleaf_checked,
            checking="session" in busy or launching,
            port=port_text(self._overleaf_url),
        )
        self._overleaf.show_view(overleaf_view(facts))
        self._overleaf_check_button.setEnabled("session" not in busy)
        self._overleaf_login_button.setEnabled("login" not in busy)
        starting = "start" in busy or "launch_start" in busy
        self._overleaf_start_button.setEnabled(not starting)
        offered = self._overleaf_engine == OVERLEAF_ENGINE_DOWN and self._overleaf_folder is not None
        self._overleaf_start_button.setVisible(offered)

    def _check_overleaf_engine(self) -> None:
        """Look at the engine's port, and whether its starter folder is known. No request goes to the engine or to Overleaf."""
        self._run_overleaf(
            "engine",
            partial(_overleaf_probe, self._deps.store, self._overleaf_deps),
            self._overleaf_engine_done,
        )

    def _overleaf_engine_done(self, value: Any, failure: str) -> None:
        if failure or not isinstance(value, OverleafProbe):
            self._overleaf_engine = OVERLEAF_ENGINE_FAILED
            self._overleaf_folder = None
            return
        self._overleaf_url = value.url
        self._overleaf_engine = OVERLEAF_ENGINE_RUNNING if value.listening else OVERLEAF_ENGINE_DOWN
        self._overleaf_folder = value.folder

    def _on_overleaf_check(self) -> None:
        """Check: asks the engine once about the saved session. This is the card's only Overleaf request, one per press."""
        self._run_overleaf(
            "session",
            partial(_overleaf_session_read, self._deps.store, self._overleaf_deps),
            self._overleaf_session_done,
        )

    def _overleaf_session_done(self, value: Any, failure: str) -> None:
        if failure or not isinstance(value, overleaf_client.SessionStatus):
            self._overleaf_session = overleaf_client.SessionStatus(
                overleaf_client.STATE_ERROR,
                error=SESSION_CHECK_FAILED_TEMPLATE.format(failure or "unknown error"),
            )
        else:
            self._overleaf_session = value
        self._overleaf_checked = datetime.now().strftime(TIME_FORMAT)
        if self._overleaf_session.state == overleaf_client.STATE_ENGINE_NOT_RUNNING:
            self._overleaf_engine = OVERLEAF_ENGINE_DOWN
        elif failure == "":
            # The engine answered, so it is running. A failed job says nothing about the engine, so the state stays.
            self._overleaf_engine = OVERLEAF_ENGINE_RUNNING

    def _on_overleaf_login(self) -> None:
        """Log in: asks the engine to open its login window. The owner signs in there, then presses Check."""
        self._run_overleaf(
            "login",
            partial(_overleaf_login_read, self._deps.store, self._overleaf_deps),
            self._overleaf_login_done,
        )

    def _overleaf_login_done(self, value: Any, failure: str) -> None:
        if failure or not isinstance(value, overleaf_client.LoginStatus):
            self._overleaf.set_note(LOGIN_FAILED_TEXT)
            return
        if value.reason == overleaf_client.REASON_ENGINE_NOT_RUNNING:
            self._overleaf_engine = OVERLEAF_ENGINE_DOWN
        elif value.started or value.reason in overleaf_client.LOGIN_REASONS:
            self._overleaf_engine = OVERLEAF_ENGINE_RUNNING
        self._overleaf.set_note(overleaf_login_text(value))

    def _on_overleaf_start(self) -> None:
        """Start engine: asks first, and No is the default. Only a yes runs the engine's starter, which then runs in the
        background, and the card waits a few seconds for the engine's port."""
        folder = self._overleaf_folder
        if folder is None:
            return
        question = START_QUESTION.format(address_text(self._overleaf_url) or "the engine address")
        if not self._overleaf_deps.confirm(self, START_TITLE, question):
            self._overleaf.set_note(START_CANCELLED_TEXT)
            return
        self._overleaf.set_note(START_WORKING_TEXT)
        self._run_overleaf(
            "start",
            partial(_overleaf_start_read, self._deps.store, self._overleaf_deps, folder),
            self._overleaf_start_done,
        )

    def _overleaf_start_done(self, value: Any, failure: str) -> None:
        if failure or not isinstance(value, tuple) or len(value) != 2:
            self._overleaf_engine = OVERLEAF_ENGINE_DOWN
            self._overleaf.set_note(START_FAILED_TEMPLATE.format(failure or "no result"))
            return
        running, reason = value
        self._overleaf_engine = OVERLEAF_ENGINE_RUNNING if running else OVERLEAF_ENGINE_DOWN
        self._overleaf.set_note(overleaf_start_text(bool(running), str(reason), self._overleaf_deps.wait_seconds))

    # ------------------------------------------------------------ the Overleaf (LaTeX) card: launch, switches and folder

    def run_launch_checks(self) -> None:
        """The launch-time checks. app.py calls this once, after the window shows. The first call runs them; a later call,
        a show or the Home timer does nothing, so they never repeat within a launch.

        Each step runs only when its own switch is on, and nothing here asks the owner. The engine is started when it is
        down and the owner has chosen its folder (engine_auto_start). The saved session is checked once when the engine is
        running (overleaf_auto_check). With both switches off, nothing runs.
        """
        if self._launch_checks_ran:
            return
        self._launch_checks_ran = True
        settings = self._deps.store.load().settings
        if not (settings.engine_auto_start or settings.overleaf_auto_check):
            return
        self._run_overleaf(
            "launch",
            partial(_overleaf_probe, self._deps.store, self._overleaf_deps),
            self._overleaf_launch_probed,
        )

    def _overleaf_launch_probed(self, value: Any, failure: str) -> None:
        """The engine's port and folder, read at launch. Then the one session check if the engine is up, or else the start."""
        self._overleaf_engine_done(value, failure)
        if failure or not isinstance(value, OverleafProbe):
            return
        settings = self._deps.store.load().settings
        if value.listening:
            self._check_session_at_launch()
            return
        if not settings.engine_auto_start:
            return
        if value.folder is None:
            self._overleaf.set_note(ENGINE_FOLDER_NOT_SET_TEXT)
            return
        self._overleaf.set_note(LAUNCH_STARTING_TEXT)
        self._run_overleaf(
            "launch_start",
            partial(_overleaf_start_read, self._deps.store, self._overleaf_deps, value.folder),
            self._overleaf_launch_started,
        )

    def _overleaf_launch_started(self, value: Any, failure: str) -> None:
        """The launch-time start has run and waited for the port. The engine's state and the line follow, and the session is
        checked once when the engine is up."""
        running, reason = False, ""
        if not failure and isinstance(value, tuple) and len(value) == 2:
            running, reason = bool(value[0]), str(value[1])
        self._overleaf_engine = OVERLEAF_ENGINE_RUNNING if running else OVERLEAF_ENGINE_DOWN
        self._overleaf.set_note(launch_start_text(running, reason))
        if running:
            self._check_session_at_launch()

    def _check_session_at_launch(self) -> None:
        """The launch-time session check, when its switch is on. It is the one Overleaf request the launch makes."""
        if not self._deps.store.load().settings.overleaf_auto_check:
            return
        self._run_overleaf(
            "session",
            partial(_overleaf_session_read, self._deps.store, self._overleaf_deps),
            self._overleaf_session_done,
        )

    def _sync_overleaf_options(self) -> None:
        """Set the two switches to the saved values. Their signals are blocked meanwhile, so nothing is written."""
        settings = self._deps.store.load().settings
        pairs = (
            (self._overleaf_auto_start_box, settings.engine_auto_start),
            (self._overleaf_auto_check_box, settings.overleaf_auto_check),
        )
        for box, value in pairs:
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)

    def _on_overleaf_switch(self, key: str, checked: bool) -> None:
        """A switch was clicked: its value is saved at once, and every other field is kept. A write that fails says so, and
        the switch goes back to the saved value."""
        try:
            self._deps.store.save(replace(self._deps.store.load().settings, **{key: checked}))
        except (OSError, ValueError):
            self._overleaf.set_note(OPTION_NOT_SAVED_TEXT)
            self._sync_overleaf_options()

    def _on_overleaf_choose_folder(self) -> None:
        """Choose engine folder: the folder is saved only when it holds the engine's starter. Choosing starts nothing."""
        settings = self._deps.store.load().settings
        chosen = self._overleaf_deps.choose_folder(self, settings.engine_folder)
        if not chosen:
            return
        if not overleaf_engine.has_starter(Path(chosen)):
            self._overleaf.set_note(NO_STARTER_CHOSEN_TEXT)
            return
        try:
            self._deps.store.save(replace(settings, engine_folder=chosen))
        except (OSError, ValueError):
            self._overleaf.set_note(OPTION_NOT_SAVED_TEXT)
            return
        self._overleaf_folder = Path(chosen)
        self._overleaf.set_note(ENGINE_FOLDER_SAVED_TEXT)
        self._refresh_overleaf()

    # ------------------------------------------------------------ events

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._timer.start()
        self.refresh_tunnel_gate()
        self._sync_overleaf_options()
        stale = self._last_run is None or time.monotonic() - self._last_run >= CHECK_INTERVAL_SECONDS
        if stale:
            self._show_timer.stop()
            self.check_all()
        else:
            self._show_timer.start()
        # The engine's port is looked at each time Home is shown. The Overleaf session is not read here.
        self._check_overleaf_engine()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        self._timer.stop()
        self._show_timer.stop()


__all__ = [
    "CHECK_INTERVAL_MS",
    "CHECK_INTERVAL_SECONDS",
    "CHECK_NOW_TEXT",
    "SHOW_DEBOUNCE_MS",
    "START_WAIT_SECONDS",
    "STALL_SECONDS",
    "HomeDeps",
    "HomeTab",
    "OverleafDeps",
]
