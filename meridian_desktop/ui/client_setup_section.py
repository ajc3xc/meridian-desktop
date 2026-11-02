"""Client setup on the Settings page: the Meridian core checkout, its Python, and one row per client.

Each row has a box the owner ticks to include that client, the client's settings file, and its state. Only ticked
clients are touched: Check setup plans the ticked clients, and Apply and Remove write them. Each box saves at once.
Check setup runs the core's plan and changes nothing. Apply and Remove first run a dry run for the ticked clients, so the
confirmation dialog names each client's exact file, the exact change and the exact command. Only a Yes runs the change,
and the core makes a backup of each file first. The result line then shows the backup paths and whether a restart is needed.
A client the core could not set up shows its own message on its own row.

Every command runs on the thread pool, so the window stays responsive. The core checkout and Python are typed or chosen here
and saved through settings_store, which refuses a path that does not exist. The top of the section says what the desktop
found on this computer (ui/runner_actions.py, RunnerActions.find_core), with Find it again, and Choose the Meridian core folder
when nothing usable was found. The owner does not have to type either path. The runner and the dialogs are injected, so
the tests never start the real command or open a real dialog.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.client_setup_runner import (
    CLIENTS,
    FAILED_ACTION,
    KIND_FAILED,
    LABELS,
    NOT_INSTALLED_ACTION,
    NOT_INSTALLED_TEXT,
    NOT_SELECTED,
    NOT_SELECTED_TEXT,
    NOT_SET_CHECKOUT_TEXT,
    NOT_SET_PYTHON_TEXT,
    STATE_TEXT,
    CheckResult,
    ClientChange,
    ClientRow,
    ClientSetupRunner,
    CommandResult,
    covered_ticked,
    names_text,
    row_from_change,
    ticked_clients,
    unchecked_rows,
)
from meridian_desktop.core_locator import LocatedCore
from meridian_desktop.settings_store import CORE_CHECKOUT_KEY, CORE_PYTHON_KEY, SettingsStore
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.text import TextBlock, make_box_selectable, make_selectable

HEADING_TEXT = "Client setup"
FIND_AGAIN_TEXT = "Find it again"
CHOOSE_CORE_TEXT = "Choose the Meridian core folder"
LOCATING_TEXT = "Looking for the Meridian core on this computer. This takes a few seconds."
NOT_LOOKED_TEXT = "The Meridian core has not been looked for yet in this run."
FOUND_TEXT = "Meridian core found on this computer: {checkout}. Its Python: {python}."
NOT_FOUND_TEXT = (
    "The Meridian core was not found. {reason} Choose the Meridian core folder, "
    "or press Find it again after you move or install it."
)
INTRO_TEXT = (
    "Writes Meridian's local tools into the settings file of each client you tick. Check setup changes nothing. "
    "Apply and Remove ask first, and a backup of each file is made before anything is written."
)
CHOICES_TEXT = "Clients to set up:"
CHECKOUT_LABEL = "Meridian core checkout:"
PYTHON_LABEL = "Python for Meridian core:"
BROWSE_TEXT = "Browse..."
SAVE_TEXT = "Save"
CHECK_TEXT = "Check setup"
APPLY_TEXT = "Apply"
REMOVE_TEXT = "Remove"
CHECKOUT_DIALOG_TITLE = "Choose the folder that holds the meridian package"
PYTHON_DIALOG_TITLE = "Choose the Python that can import meridian"
PYTHON_FILE_FILTER = "Python program (python.exe python python3);;All files (*)"
YES_TEXT = "Yes"
NO_TEXT = "No"
APPLY_DIALOG_TITLE = "Apply Meridian's entries?"
REMOVE_DIALOG_TITLE = "Remove Meridian's entries?"
DEFAULT_STATUS_TEXT = "Press Check setup to read each ticked client's settings file. Nothing is written."
NO_TICK_STATUS_TEXT = "No client is ticked. Tick the clients to set up. Nothing is written."
CHECKING_TEXT = "Checking the clients' settings files. Nothing is written."
CHECKOUT_SAVED_TEXT = "Meridian core checkout saved."
LAUNCH_STARTS_OFF_TEXT = (
    "The Zotero and local server switches are now off, because the core setup changed. "
    "Turn them on again to confirm the new setup."
)
PYTHON_SAVED_TEXT = "Python for Meridian core saved."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written."
CANCELLED_TEXT = "Cancelled. Nothing was changed."
NOTHING_CHANGED_TEXT = "Nothing was changed."
ALREADY_IN_PLACE_TEXT = "{label} is already in place. Nothing was changed."
NOTHING_TO_REMOVE_TEXT = "No Meridian entries are in {path}. Nothing was changed."
WRITTEN_TEXT = "Written: {path}."
REMOVED_TEXT = "Removed Meridian's entries from {path}."
NO_BACKUP_TEXT = "No backup was made: the file did not exist before this change."
BACKUP_TEXT = "Backup saved at: {backup}"
RESTART_TEXT = "Restart {label} to load the change."
NO_FILE_TEXT = "No settings file is written for this client yet."
PATH_UNKNOWN_TEXT = "File: not known until Check setup reads it."
FILE_PREFIX = "File: "
REASON_NOT_SET = "Set the Meridian core checkout and Python first, then save them."
REASON_NOT_TICKED = "Tick at least one client first."
REASON_NOT_COVERED = "The Meridian core does not cover the ticked clients yet."
TOOLTIP_CHECK_FIRST = "Press Check setup first."

_CHECK = "check"
_PREVIEW = "preview"
_CHANGE = "change"

FileChooser = Callable[[QWidget, str], str]
FolderChooser = Callable[[QWidget, str], str]
Confirm = Callable[[QWidget, str, str], bool]


def _join_labels(labels: Sequence[str]) -> str:
    """'A', 'A and B', or 'A, B and C'."""
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def confirmation_text(mode: str, entries: Sequence[tuple[str, ClientChange]], command: str) -> str:
    """The confirmation dialog's text: each client's exact file and change, the exact command, and what is left alone."""
    labels = _join_labels([label for label, _change in entries])
    if mode == "apply":
        heading = f"Apply Meridian's entries to {labels}?"
    else:
        heading = f"Remove Meridian's entries from {labels}?"
    lines = [heading]
    for label, change in entries:
        lines.append(f"{label}:")
        lines.append(f"File: {change.path}")
        lines.append(f"Change: {names_text(change)}")
    lines.append("Entries that Meridian does not own are left alone. A backup of each file is made first.")
    lines.extend(RESTART_TEXT.format(label=label) for label, _change in entries)
    lines.append(f"Command: {command}")
    return "\n".join(lines)


def result_text(mode: str, label: str, change: ClientChange) -> str:
    """The result lines after a real change: the file, the backup path, and a restart note when needed."""
    lines = [
        (REMOVED_TEXT if mode == "remove" else WRITTEN_TEXT).format(path=change.path),
        BACKUP_TEXT.format(backup=change.backup) if change.backup else NO_BACKUP_TEXT,
    ]
    if change.restart_required:
        lines.append(RESTART_TEXT.format(label=label))
    return "\n".join(lines)


def no_change_text(mode: str, label: str, path: str) -> str:
    """The result line when the core reports that there was nothing to change."""
    if mode == "remove":
        return NOTHING_TO_REMOVE_TEXT.format(path=path)
    return ALREADY_IN_PLACE_TEXT.format(label=label)


def _action_reason(configured: bool, ticked: Sequence[str], covered: Sequence[str]) -> str:
    """The tooltip for a disabled Apply or Remove. Empty when the buttons are enabled."""
    if not configured:
        return REASON_NOT_SET
    if not ticked:
        return REASON_NOT_TICKED
    if not covered:
        return REASON_NOT_COVERED
    return ""


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _button(parent: QWidget, name: str, text: str) -> QPushButton:
    button = QPushButton(text, parent)
    button.setObjectName(name)
    return button


def _set_message(label: QLabel, text: str) -> None:
    """Show a message line, or hide the line when the text is empty."""
    label.setText(text)
    label.setVisible(bool(text))


def _choose_checkout_folder(parent: QWidget, start_dir: str) -> str:
    """Ask for a folder. Return its path, or "" when the dialog is cancelled."""
    return QFileDialog.getExistingDirectory(parent, CHECKOUT_DIALOG_TITLE, start_dir)


def _choose_python_file(parent: QWidget, start_dir: str) -> str:
    """Ask for a Python program. Return its path, or "" when the dialog is cancelled."""
    path, _selected_filter = QFileDialog.getOpenFileName(parent, PYTHON_DIALOG_TITLE, start_dir, PYTHON_FILE_FILTER)
    return path


def _ask_yes_no(parent: QWidget, title: str, text: str) -> bool:
    """A modal Yes or No. No is the default and Escape, and only a click on Yes returns True."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    yes = box.addButton(YES_TEXT, QMessageBox.ButtonRole.YesRole)
    no = box.addButton(NO_TEXT, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    # The windows11 style lets the owner click only links in a message box, so its text is made selectable here.
    make_box_selectable(box)
    box.exec()
    return box.clickedButton() is yes


@dataclass(frozen=True)
class _Pending:
    """What a background command was started for. It is handed back with the result.

    clients is the ticked clients the command was started for, in display order.
    """

    purpose: str
    clients: tuple[str, ...]
    mode: str


@dataclass(frozen=True)
class _Done:
    """One finished command, as its job hands it back: its generation, what it was started for, and its result."""

    generation: int
    pending: _Pending
    payload: object


class _CommandJob(QRunnable):
    """Run one command on the thread pool and deliver a _Done to the shared delivery object.

    The job holds only the command, the owner token, the generation and the shared delivery object. It never holds the
    section.
    """

    def __init__(
        self,
        generation: int,
        pending: _Pending,
        work: Callable[[], object],
        owner: object,
        delivery: Delivery,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._pending = pending
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        try:
            payload = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            # The class name only: an exception's text could hold a path or a value.
            text = f"The Meridian setup step failed ({exc.__class__.__name__})."
            if self._pending.purpose == _CHECK:
                payload = CheckResult(rows=unchecked_rows(self._pending.clients), message=text)
            else:
                payload = CommandResult(False, KIND_FAILED, text)
        self._delivery.delivered.emit(Delivered(self._owner, _Done(self._generation, self._pending, payload)))


@dataclass
class _RowWidgets:
    choice: QCheckBox
    state: QLabel
    path: QLabel
    detail: QLabel


class ClientSetupSection(QWidget):
    """The client setup block for the Settings page. One section, one row per client, and Check, Apply and Remove.

    ``coreChanged`` is emitted after a core path is saved, or after the core search changes the setup, so the runner
    block on the same tab shows its switches and its local state again. The boxes follow the saved paths, except a box the
    owner has typed into, which is left as it is.
    """

    coreChanged = Signal()

    def __init__(
        self,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
        *,
        runner: ClientSetupRunner | None = None,
        choose_folder: FolderChooser = _choose_checkout_folder,
        choose_python: FileChooser = _choose_python_file,
        confirm: Confirm = _ask_yes_no,
        actions: RunnerActions | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("client-setup-section")
        self._store = settings_store
        self._runner = runner if runner is not None else ClientSetupRunner(settings_store)
        self._choose_folder = choose_folder
        self._choose_python = choose_python
        self._confirm = confirm
        self._actions = actions if actions is not None else RunnerActions(settings_store)
        # The saved values the two boxes last showed. A box that still shows one follows the saved value; any other is typed.
        self._shown_checkout = ""
        self._shown_python = ""
        # The clients the owner has ticked, as saved. Only these are checked, applied or removed.
        self._choices: frozenset[str] = frozenset()
        self._rows: dict[str, ClientRow] = {row.client: row for row in unchecked_rows()}
        self._widgets: dict[str, _RowWidgets] = {}
        self._busy = False
        self._configured = False
        self._missing = ""
        self._last_status = DEFAULT_STATUS_TEXT
        # Identifies this section's commands. A plain object, so a job can hold it without holding the section.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._actions.coreChanged.connect(self._on_core_changed)
        self._build()
        self.refresh()
        self._render_rows()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # The heading, the intro and what the search found are one block, so one drag over them selects all three. Settings
        # hides the heading, which repeats its group title. The two buttons that search again or let the owner choose the
        # folder sit below the block.
        intro = TextBlock(self, "client-setup-intro-text", spacing=layout.spacing())
        intro.line("client-setup-heading", HEADING_TEXT)
        intro.line("client-setup-intro", INTRO_TEXT)
        self._finder_status = intro.line("client-setup-finder-status", "")
        layout.addWidget(intro.label)
        finder_row = QHBoxLayout()
        self._find_again = _button(self, "client-setup-find-again", FIND_AGAIN_TEXT)
        self._choose_core = _button(self, "client-setup-choose-core", CHOOSE_CORE_TEXT)
        finder_row.addWidget(self._find_again)
        finder_row.addWidget(self._choose_core)
        finder_row.addStretch(1)
        layout.addLayout(finder_row)
        # The two path rows share one grid: the caption column takes the width of its longest caption, and the path box
        # takes the rest. A caption never wraps, so "Meridian core checkout:" is always whole.
        paths = QGridLayout()
        paths.setContentsMargins(0, 0, 0, 0)
        paths.setHorizontalSpacing(8)
        paths.setVerticalSpacing(6)
        paths.setColumnStretch(1, 1)

        self._checkout_input = QLineEdit(self)
        self._checkout_input.setObjectName("client-setup-checkout")
        self._browse_checkout = _button(self, "client-setup-browse-checkout", BROWSE_TEXT)
        self._save_checkout = _button(self, "client-setup-save-checkout", SAVE_TEXT)
        self._path_row(
            paths,
            0,
            _label(self, "client-setup-checkout-label", CHECKOUT_LABEL),
            self._checkout_input,
            self._browse_checkout,
            self._save_checkout,
        )

        self._python_input = QLineEdit(self)
        self._python_input.setObjectName("client-setup-python")
        self._browse_python = _button(self, "client-setup-browse-python", BROWSE_TEXT)
        self._save_python = _button(self, "client-setup-save-python", SAVE_TEXT)
        self._path_row(
            paths,
            1,
            _label(self, "client-setup-python-label", PYTHON_LABEL),
            self._python_input,
            self._browse_python,
            self._save_python,
        )
        layout.addLayout(paths)

        self._settings_message = _label(self, "client-setup-settings-message")
        _set_message(self._settings_message, "")
        layout.addWidget(self._settings_message)

        check_row = QHBoxLayout()
        self._check_button = _button(self, "client-setup-check", CHECK_TEXT)
        check_row.addWidget(self._check_button)
        check_row.addStretch(1)
        layout.addLayout(check_row)

        self._status = _label(self, "client-setup-status")
        layout.addWidget(self._status)

        choices_caption = _label(self, "client-setup-choices-caption", CHOICES_TEXT)
        layout.addWidget(choices_caption)
        for client, label in CLIENTS:
            layout.addLayout(self._client_block(client, label))

        action_row = QHBoxLayout()
        self._apply_all = _button(self, "client-setup-apply", APPLY_TEXT)
        self._remove_all = _button(self, "client-setup-remove", REMOVE_TEXT)
        action_row.addWidget(self._apply_all)
        action_row.addWidget(self._remove_all)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._result = _label(self, "client-setup-result")
        _set_message(self._result, "")
        layout.addWidget(self._result)

        self._browse_checkout.clicked.connect(self._on_browse_checkout)
        self._browse_python.clicked.connect(self._on_browse_python)
        self._save_checkout.clicked.connect(self._on_save_checkout)
        self._save_python.clicked.connect(self._on_save_python)
        self._check_button.clicked.connect(self._on_check_clicked)
        self._apply_all.clicked.connect(self._on_apply_clicked)
        self._remove_all.clicked.connect(self._on_remove_clicked)
        self._find_again.clicked.connect(self._on_find_again_clicked)
        self._choose_core.clicked.connect(self._on_choose_core_clicked)

    def _path_row(
        self, grid: QGridLayout, row: int, label: QLabel, line: QLineEdit, browse: QPushButton, save: QPushButton
    ) -> None:
        """One path row in the shared grid: caption, path box, Browse and Save. The caption never wraps."""
        label.setWordWrap(False)
        grid.addWidget(label, row, 0)
        grid.addWidget(line, row, 1)
        grid.addWidget(browse, row, 2)
        grid.addWidget(save, row, 3)

    def _client_block(self, client: str, label: str) -> QVBoxLayout:
        block = QVBoxLayout()
        head = QHBoxLayout()
        choice = QCheckBox(label, self)
        choice.setObjectName(f"client-setup-{client}-choice")
        head.addWidget(choice)
        state = _label(self, f"client-setup-{client}-state")
        head.addWidget(state, 1)
        block.addLayout(head)
        # The path and the detail line are one block, so one drag over them selects both. The gap is the 6 px a layout
        # gives two labels on this style. The block layout has no parent yet, so its own spacing() is unset here and
        # cannot supply it.
        text = TextBlock(self, f"client-setup-{client}-text", spacing=6)
        path = text.line(f"client-setup-{client}-path", "")
        detail = text.line(f"client-setup-{client}-detail", "")
        _set_message(detail, "")
        block.addWidget(text.label)

        self._widgets[client] = _RowWidgets(choice, state, path, detail)
        choice.toggled.connect(lambda checked, c=client: self._on_choice_toggled(c, bool(checked)))
        return block

    # ----------------------------------------------------------- state on screen

    def refresh(self) -> None:
        """Show the saved core checkout, Python and ticked clients, then update the status and the buttons."""
        settings = self._store.load().settings
        self._checkout_input.setText(settings.core_checkout)
        self._python_input.setText(settings.core_python)
        self._shown_checkout = settings.core_checkout
        self._shown_python = settings.core_python
        self._choices = frozenset(settings.client_choices)
        self._sync_choice_boxes()
        self._recompute()
        self._render_finder()
        self._render_rows()

    def _sync_choice_boxes(self) -> None:
        """Tick the boxes the saved choices name. Showing the saved value must not save it again."""
        for client, _label in CLIENTS:
            box = self._widgets[client].choice
            box.blockSignals(True)
            box.setChecked(client in self._choices)
            box.blockSignals(False)

    def _follow_saved_boxes(self) -> None:
        """Move a box to a new saved path, unless the owner has typed into it since it last showed a saved path."""
        settings = self._store.load().settings
        if self._checkout_input.text().strip() == self._shown_checkout:
            self._checkout_input.setText(settings.core_checkout)
            self._shown_checkout = settings.core_checkout
        if self._python_input.text().strip() == self._shown_python:
            self._python_input.setText(settings.core_python)
            self._shown_python = settings.core_python

    def _render_finder(self) -> None:
        """Say what the search found on this computer, in plain words."""
        located: LocatedCore | None = self._actions.located
        if self._actions.locating:
            text = LOCATING_TEXT
        elif located is None:
            text = NOT_LOOKED_TEXT
        elif located.found:
            text = FOUND_TEXT.format(checkout=located.checkout, python=located.python)
        else:
            text = NOT_FOUND_TEXT.format(reason=located.reason)
        _set_message(self._finder_status, text)

    def _choose_core_needed(self) -> bool:
        """The Choose button shows while the setup is not usable: the search found nothing, or the saved setup is not set."""
        located = self._actions.located
        if located is not None and not located.found:
            return True
        return not self._actions.core().core_ready

    def _recompute(self) -> None:
        """Say plainly what is not set yet, and turn the buttons on or off. The boxes are left as they are."""
        settings = self._store.load().settings
        if not settings.core_checkout:
            self._missing = NOT_SET_CHECKOUT_TEXT
        elif not settings.core_python:
            self._missing = NOT_SET_PYTHON_TEXT
        else:
            self._missing = ""
        self._configured = not self._missing
        self._refresh_status()
        self._apply_enablement()

    def _refresh_status(self) -> None:
        if self._missing:
            text = self._missing
        elif not ticked_clients(self._choices):
            text = NO_TICK_STATUS_TEXT
        else:
            text = self._last_status
        _set_message(self._status, text)

    def _display_row(self, client: str, label: str) -> ClientRow:
        """The row to show. A client that is not ticked shows as Not selected, whatever was read for it before."""
        if client not in self._choices:
            return ClientRow(client, label, NOT_SELECTED, detail=NOT_SELECTED_TEXT)
        return self._rows[client]

    def _render_rows(self) -> None:
        for client, label in CLIENTS:
            row = self._display_row(client, label)
            widgets = self._widgets[client]
            widgets.state.setText(STATE_TEXT[row.state])
            if row.state == NOT_SELECTED:
                path_text = ""
            elif not row.covered:
                path_text = NO_FILE_TEXT
            elif row.path:
                path_text = FILE_PREFIX + row.path
            else:
                path_text = PATH_UNKNOWN_TEXT
            _set_message(widgets.path, path_text)
            _set_message(widgets.detail, row.detail)

    def _apply_enablement(self) -> None:
        busy = self._busy
        for widget in (
            self._checkout_input,
            self._python_input,
            self._browse_checkout,
            self._save_checkout,
            self._browse_python,
            self._save_python,
        ):
            widget.setEnabled(not busy)
        for widgets in self._widgets.values():
            widgets.choice.setEnabled(not busy)
        self._check_button.setEnabled(not busy)
        searching = self._actions.locating
        self._find_again.setEnabled(not busy and not searching)
        self._choose_core.setEnabled(not busy and not searching)
        self._choose_core.setVisible(self._choose_core_needed())
        reason = _action_reason(self._configured, ticked_clients(self._choices), covered_ticked(self._choices))
        for button in (self._apply_all, self._remove_all):
            button.setEnabled(not busy and not reason)
            button.setToolTip(reason)

    # ----------------------------------------------------------- the clients the owner ticks

    def _on_choice_toggled(self, client: str, checked: bool) -> None:
        """Save the owner's tick at once. A save that is refused puts the box back and says why."""
        names = set(self._choices)
        if checked:
            names.add(client)
        else:
            names.discard(client)
        try:
            saved = self._store.set_client_choices(ticked_clients(names))
        except (ValueError, OSError):
            _set_message(self._settings_message, NOT_WRITTEN_TEXT)
            self._sync_choice_boxes()
            return
        self._choices = frozenset(saved.client_choices)
        if client in self._choices:
            # A client just ticked shows the state it has not been checked for yet, not an old result.
            self._rows[client] = next(row for row in unchecked_rows(self._choices) if row.client == client)
        self._render_rows()
        self._recompute()

    # ----------------------------------------------------------- saving the core paths

    @Slot()
    def _on_save_checkout(self) -> None:
        self._save_core(CORE_CHECKOUT_KEY, self._checkout_input.text(), CHECKOUT_SAVED_TEXT)

    @Slot()
    def _on_save_python(self) -> None:
        self._save_core(CORE_PYTHON_KEY, self._python_input.text(), PYTHON_SAVED_TEXT)

    def _save_core(self, key: str, value: str, saved_text: str) -> None:
        before = self._store.load().settings
        try:
            saved = self._store.set_core_path(key, value)
        except ValueError as exc:
            # The store's text is fixed wording that names the path it refused, and nothing was written.
            _set_message(self._settings_message, str(exc))
            return
        except OSError:
            _set_message(self._settings_message, NOT_WRITTEN_TEXT)
            return
        # The box that was just saved now shows a saved path, so a later search may move it.
        if key == CORE_CHECKOUT_KEY:
            self._shown_checkout = saved.core_checkout
        else:
            self._shown_python = saved.core_python
        # A saved core path turns both launch-time switches off (contract amendment C), and the status line says so.
        turned_off = before.zotero_autostart or before.local_server_autostart or before.tunnel_autostart
        _set_message(self._settings_message, f"{saved_text} {LAUNCH_STARTS_OFF_TEXT}" if turned_off else saved_text)
        self._recompute()  # the other box keeps what the user typed, saved or not
        self.coreChanged.emit()  # the runner block on this tab shows the switches as they are saved now
        self._actions.notify_core_changed()  # the Tunnel and local controls read the saved setup again

    @Slot()
    def _on_find_again_clicked(self) -> None:
        """Search this computer again. The search runs on the thread pool, and its result comes back through _on_core_changed."""
        self._actions.find_core()

    @Slot()
    def _on_choose_core_clicked(self) -> None:
        _set_message(self._settings_message, self._actions.choose_core_folder(self))

    @Slot()
    def _on_core_changed(self) -> None:
        """The core search finished or started, or the core folder changed. Show the search result and the saved paths."""
        self._follow_saved_boxes()
        self._recompute()
        self._render_finder()
        self.coreChanged.emit()  # the runner block on this tab reads its switches and local state again

    @Slot()
    def _on_browse_checkout(self) -> None:
        chosen = self._choose_folder(self, self._checkout_input.text().strip())
        if chosen:
            self._checkout_input.setText(chosen)  # not saved until Save is pressed

    @Slot()
    def _on_browse_python(self) -> None:
        chosen = self._choose_python(self, self._python_input.text().strip())
        if chosen:
            self._python_input.setText(chosen)  # not saved until Save is pressed

    # ----------------------------------------------------------- background commands

    def _start(self, pending: _Pending, work: Callable[[], object]) -> None:
        self._busy = True
        self._generation += 1
        self._apply_enablement()
        # The job holds the command, the owner token, the generation and the shared delivery object. It never holds the section.
        QThreadPool.globalInstance().start(_CommandJob(self._generation, pending, work, self._owner, shared_delivery()))

    @Slot()
    def _on_check_clicked(self) -> None:
        _set_message(self._status, CHECKING_TEXT)
        clients = ticked_clients(self._choices)
        runner = self._runner
        self._start(_Pending(_CHECK, clients, ""), lambda: runner.check(clients))

    @Slot()
    def _on_apply_clicked(self) -> None:
        self._start_preview("apply")

    @Slot()
    def _on_remove_clicked(self) -> None:
        self._start_preview("remove")

    def _start_preview(self, mode: str) -> None:
        """Dry run first, for the ticked clients. The dialog then shows exactly what a Yes would write."""
        clients = ticked_clients(self._choices)
        runner = self._runner
        self._start(_Pending(_PREVIEW, clients, mode), lambda: runner.preview(clients, mode))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another section is dropped here, and so is one from an earlier command.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _Done) or done.generation != self._generation:
            return
        pending, payload = done.pending, done.payload
        self._busy = False
        self._apply_enablement()
        if pending.purpose == _CHECK:
            self._show_check(payload)
        elif pending.purpose == _PREVIEW:
            self._after_preview(pending, payload)
        else:
            self._after_change(pending, payload)

    def _show_check(self, result: object) -> None:
        assert isinstance(result, CheckResult)
        for row in result.rows:
            self._rows[row.client] = row
        self._render_rows()
        self._last_status = result.message or DEFAULT_STATUS_TEXT
        self._refresh_status()
        self._apply_enablement()

    def _show_own_row(self, change: ClientChange) -> None:
        """A client the core could not set up, or whose folder is missing, shows its own row, and no other row."""
        self._rows[change.client] = row_from_change(change, LABELS.get(change.client, change.client))
        self._render_rows()

    def _after_preview(self, pending: _Pending, result: object) -> None:
        assert isinstance(result, CommandResult)
        if not result.ok:
            _set_message(self._result, result.message)
            return
        mode = pending.mode
        for change in result.changes:
            if change.action in (FAILED_ACTION, NOT_INSTALLED_ACTION):
                self._show_own_row(change)
        unchanged = [change for change in result.changes if change.action == "unchanged"]
        missing = [change for change in result.changes if change.action == NOT_INSTALLED_ACTION]
        # A client whose folder is missing has nothing to write, so it is left out of the confirmation and its command.
        to_write = [
            change for change in result.changes if change.action not in ("unchanged", FAILED_ACTION, NOT_INSTALLED_ACTION)
        ]
        if not to_write:
            notes = [no_change_text(mode, LABELS.get(change.client, change.client), change.path) for change in unchanged]
            notes += [NOT_INSTALLED_TEXT.format(label=LABELS.get(change.client, change.client)) for change in missing]
            _set_message(self._result, "\n".join(notes) if notes else NOTHING_CHANGED_TEXT)
            return
        entries = [(LABELS.get(change.client, change.client), change) for change in to_write]
        clients = tuple(change.client for change in to_write)
        command = self._runner.describe_command(clients, mode)
        title = APPLY_DIALOG_TITLE if mode == "apply" else REMOVE_DIALOG_TITLE
        if not self._confirm(self, title, confirmation_text(mode, entries, command)):
            _set_message(self._result, CANCELLED_TEXT)
            return
        runner = self._runner
        self._start(_Pending(_CHANGE, clients, mode), lambda: runner.run_change(clients, mode))

    def _after_change(self, pending: _Pending, result: object) -> None:
        assert isinstance(result, CommandResult)
        # The change may have written a client's file, whatever it reports, so Home reads its Tools card again.
        self._actions.notify_client_setup_changed()
        if not result.ok:
            _set_message(self._result, result.message)
            return
        blocks: list[str] = []
        for change in result.changes:
            label = LABELS.get(change.client, change.client)
            if change.action in (FAILED_ACTION, NOT_INSTALLED_ACTION):
                self._show_own_row(change)
                if change.action == NOT_INSTALLED_ACTION:
                    blocks.append(NOT_INSTALLED_TEXT.format(label=label))
            elif change.action == "unchanged":
                blocks.append(no_change_text(pending.mode, label, change.path))
            else:
                blocks.append(result_text(pending.mode, label, change))
        _set_message(self._result, "\n".join(blocks) if blocks else NOTHING_CHANGED_TEXT)
        # Read the files again, so the rows show their new state.
        _set_message(self._status, CHECKING_TEXT)
        clients = ticked_clients(self._choices)
        runner = self._runner
        self._start(_Pending(_CHECK, clients, ""), lambda: runner.check(clients))


__all__ = [
    "ClientSetupSection",
    "confirmation_text",
    "no_change_text",
    "result_text",
]
