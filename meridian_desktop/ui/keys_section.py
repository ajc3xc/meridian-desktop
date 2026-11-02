"""The Keys block on the Settings tab: RunPod, a search box, one card per saved name, and an add form. Qt code.

The RunPod entry is the RunPod panel's own card (ui/runpod_panel.py), so its checks, its pod list and its key row work as
they did before. Every other name is a card of its own: its status, a password box for a new value, Save value and Remove.
A name is added with the form at the foot of the block. The search box above the list shows only the cards whose name
contains its text, ignoring case. It searches names only, never values or statuses.

Each card, RunPod's included, has a Mark critical box and a Set as Windows user variable button. A critical name is listed
first and shows a Critical badge. The mark is saved in settings by name. Setting a user variable asks first, and then writes
the saved value to the Windows user environment.

Values go to the keychain through api_keys. This block never shows a value, never reads one back into a control, and never
writes one to the settings file. A status is read only to test whether a value is set. A failed read shows as unreadable.
Remove asks first, because the saved value is deleted from the keychain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.api_keys import (
    BUILT_IN_NAMES,
    RUNPOD_NAME,
    STATUS_NOT_SET,
    STATUS_SET,
    STATUS_UNREADABLE,
    ApiKeys,
    KeyNameError,
    slug,
)
from meridian_desktop.secret_store import SecretStoreError
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import submit
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.user_environment import UserEnvironmentError

INTRO_TEXT = (
    "Keys are kept in the OS keychain, one entry for each name. This list shows each name and whether a value is set. "
    "A value is never shown here. Type a new value to replace one."
)
ADD_HEADING_TEXT = "Add a key"
NAME_PLACEHOLDER = "Name, for example OpenAI"
VALUE_PLACEHOLDER = "Key value"
NEW_VALUE_PLACEHOLDER = "New value, to replace the saved one"
SEARCH_PLACEHOLDER = "Search keys"
# Shown when no name matches the search. It does not repeat the search text, so nothing the owner typed is echoed back.
SEARCH_EMPTY_TEXT = "No key name matches the search. Clear the search to see every name."
ADD_TEXT = "Add key"
SAVE_VALUE_TEXT = "Save value"
REMOVE_TEXT = "Remove"
REMOVE_TITLE = "Remove this key?"
REMOVE_BODY = "The saved value for {name} is deleted from the keychain. {listing} This cannot be undone."
REMOVE_LISTING = "The name also leaves the list."
CANCELLED_TEXT = "Cancelled. Nothing was removed."
STATUS_TEXT = {
    STATUS_SET: "A value is set.",
    STATUS_NOT_SET: "No value is set.",
    STATUS_UNREADABLE: "The keychain could not be read, so the state is not known.",
}
ADDED_TEXT = "Saved {name}. Its value is in the keychain and is not shown."
REPLACED_TEXT = "Saved the new value for {name}. It is not shown."
NO_VALUE_TEXT = "Type the new value first, then press Save value."
REMOVED_TEXT = "Removed {name} and its saved value."
REMOVED_NO_VALUE_TEXT = "Removed {name} from the list. No value was saved for it."
NOT_REMOVED_TEXT = "Not removed. {detail}"
# A removal that failed on the disk or in the keychain. The message names no path and no value.
NOT_REMOVED_OS_TEXT = "Not removed. The key could not be deleted or its name could not be written. Try again."
NAMES_NOT_READ_TEXT = "The saved key names could not be read. Reopen Settings to try again."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written."
RUNPOD_HEADING_TEXT = RUNPOD_NAME
CRITICAL_BADGE_TEXT = "Critical"
CRITICAL_BOX_TEXT = "Mark critical"
CRITICAL_TEXT = "{name} is marked critical. It is listed first."
UNCRITICAL_TEXT = "{name} is no longer marked critical."
USER_ENV_TEXT = "Set as Windows user variable"
USER_ENV_TITLE = "Set this key as a Windows user variable?"
# The question names the variable and says where the value goes. It never shows the value.
USER_ENV_BODY = (
    "The saved value for {name} is written to your Windows user environment as {variable}. Programs you start after this "
    "can read it. The value is not shown here. It stays there until you remove it in Windows environment settings."
)
USER_ENV_CANCELLED_TEXT = "Cancelled. Nothing was written to your Windows user environment."
USER_ENV_NO_VALUE_TEXT = "No value is saved for {name} yet. Save a value first, then set it as a Windows user variable."
USER_ENV_SET_TEXT = (
    "Set {variable} in your Windows user environment. The value is not shown. "
    "Programs already open do not see it until they restart."
)
USER_ENV_UNANNOUNCED_TEXT = "Windows did not confirm the change to running programs, so sign out and back in if one does not see it."
USER_ENV_NOT_SET_TEXT = "Not set. {detail}"
USER_ENV_OS_TEXT = "Not set. The Windows user environment could not be written. Try again."

# A confirmation: (parent, title, text) -> True only when the owner clicks Yes.
Confirm = Callable[[QWidget, str, str], bool]


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _bold(label: QLabel) -> QLabel:
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _ask_remove(parent: QWidget, title: str, text: str) -> bool:
    """The Yes or No question before a key is removed. No is the default."""
    return ask_yes_no(parent, title, text)


def _ask_user_env(parent: QWidget, title: str, text: str) -> bool:
    """The Yes or No before a value is written to the Windows user environment. No is the default."""
    return ask_yes_no(parent, title, text)


@dataclass
class _Mark:
    """The Critical badge, the Mark critical box and the user-variable button of one name. RunPod has one too."""

    badge: QLabel
    critical: QCheckBox
    user_env: QPushButton


def _show_mark(mark: _Mark, critical: bool) -> None:
    """Show the badge and set the box to the saved mark. The box's own signal does not run again for this."""
    mark.badge.setVisible(critical)
    mark.critical.blockSignals(True)
    mark.critical.setChecked(critical)
    mark.critical.blockSignals(False)


def _action_row(mark: _Mark) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(mark.critical)
    row.addStretch(1)
    row.addWidget(mark.user_env)
    return row


@dataclass
class _Card:
    """One saved name's card and the widgets the section reads and changes."""

    frame: QFrame
    status: QLabel
    value: QLineEdit
    mark: _Mark


class KeysSection(QWidget):
    """The Keys block. The RunPod card is placed by the host; every other name gets a card made here."""

    def __init__(
        self,
        keys: ApiKeys,
        runpod_card: QWidget | None = None,
        parent: QWidget | None = None,
        *,
        confirm: Confirm | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keys-section")
        self._keys = keys
        self._runpod_card = runpod_card
        # None means the Yes/No dialog. It is looked up when Remove is pressed, not here, so a test can swap it.
        self._confirm = confirm
        self._cards: dict[str, _Card] = {}
        # The RunPod card's frame and its marks, when a RunPod card is hosted. The frame sits in the same list as the others,
        # so a critical RunPod is listed first, and the search filters it by its name like the others.
        self._runpod_frame: QFrame | None = None
        self._runpod_mark: _Mark | None = None
        # The first read of the listed names runs on the thread pool, so building this block never reads the keychain.
        self._owner = object()
        self._load_generation = 0
        self._loading = False
        shared_delivery().delivered.connect(self._on_names_loaded)
        self._build()
        self._load_names()

    @property
    def loading(self) -> bool:
        """True while the listed names are being read from the keychain on the thread pool."""
        return self._loading

    def _load_names(self) -> None:
        """Read the listed names and whether each one is set, on the thread pool. A read still running is replaced."""
        self._load_generation += 1
        self._loading = True
        # The job holds the key store, never this block, so a block freed while the read runs is never touched off the GUI thread.
        keys = self._keys
        submit(self._owner, self._load_generation, lambda: keys.entries())

    @Slot(object)
    def _on_names_loaded(self, item: object) -> None:
        # Runs on the GUI thread. A result for another section, or an older read, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (entries, failure) = item.value
        if generation != self._load_generation:
            return
        self._loading = False
        if failure is not None:
            self._show(NAMES_NOT_READ_TEXT)
            return
        self._apply_entries(entries)

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(_label(self, "keys-intro", INTRO_TEXT))

        self._search = QLineEdit(self)
        self._search.setObjectName("keys-search")
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setAccessibleName(SEARCH_PLACEHOLDER)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        column.addWidget(self._search)
        self._search_empty = _label(self, "keys-search-empty", SEARCH_EMPTY_TEXT)
        self._search_empty.setVisible(False)
        column.addWidget(self._search_empty)

        # Every card sits in this one list, the RunPod card's included, so the critical names come first whichever they are.
        self._entry_layout = QVBoxLayout()
        self._entry_layout.setSpacing(8)
        column.addLayout(self._entry_layout)
        if self._runpod_card is not None:
            self._build_runpod_frame()

        self._message = _label(self, "keys-message")
        self._message.setVisible(False)
        column.addWidget(self._message)

        column.addWidget(_bold(_label(self, "keys-add-heading", ADD_HEADING_TEXT)))
        row = QHBoxLayout()
        self._new_name = QLineEdit(self)
        self._new_name.setObjectName("keys-new-name")
        self._new_name.setPlaceholderText(NAME_PLACEHOLDER)
        self._new_value = QLineEdit(self)
        self._new_value.setObjectName("keys-new-value")
        self._new_value.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_value.setPlaceholderText(VALUE_PLACEHOLDER)
        self._add_button = QPushButton(ADD_TEXT, self)
        self._add_button.setObjectName("keys-add")
        row.addWidget(self._new_name, 1)
        row.addWidget(self._new_value, 1)
        row.addWidget(self._add_button)
        column.addLayout(row)

        self._add_button.clicked.connect(self._on_add_clicked)
        self._new_value.returnPressed.connect(self._on_add_clicked)

    def _build_runpod_frame(self) -> None:
        # The RunPod card is framed like the other cards, so every name in the block looks the same.
        frame = QFrame(self)
        frame.setObjectName("keys-entry-runpod")
        self._runpod_frame = frame
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        mark = self._make_mark(frame, slug(RUNPOD_NAME), RUNPOD_NAME)
        self._runpod_mark = mark
        frame_column = QVBoxLayout(frame)
        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, "keys-runpod-name", RUNPOD_HEADING_TEXT)))
        head.addWidget(mark.badge)
        head.addStretch(1)
        frame_column.addLayout(head)
        frame_column.addWidget(self._runpod_card)
        frame_column.addLayout(_action_row(mark))
        self._entry_layout.addWidget(frame)

    def _make_mark(self, frame: QFrame, key: str, name: str) -> _Mark:
        badge = _bold(_label(frame, f"keys-entry-{key}-badge", CRITICAL_BADGE_TEXT))
        badge.setVisible(False)
        critical = QCheckBox(CRITICAL_BOX_TEXT, frame)
        critical.setObjectName(f"keys-entry-{key}-critical")
        user_env = QPushButton(USER_ENV_TEXT, frame)
        user_env.setObjectName(f"keys-entry-{key}-user-env")
        critical.toggled.connect(lambda checked, n=name: self._on_critical_toggled(n, checked))
        user_env.clicked.connect(lambda *_, n=name: self._on_user_env(n))
        return _Mark(badge, critical, user_env)

    def _make_card(self, name: str) -> _Card:
        key = slug(name)
        frame = QFrame(self)
        frame.setObjectName(f"keys-entry-{key}")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(frame)

        mark = self._make_mark(frame, key, name)
        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, f"keys-entry-{key}-name", name)))
        head.addWidget(mark.badge)
        status = _label(frame, f"keys-entry-{key}-status")
        head.addWidget(status, 1)
        column.addLayout(head)

        row = QHBoxLayout()
        value = QLineEdit(frame)
        value.setObjectName(f"keys-entry-{key}-value")
        value.setEchoMode(QLineEdit.EchoMode.Password)
        value.setPlaceholderText(NEW_VALUE_PLACEHOLDER)
        save = QPushButton(SAVE_VALUE_TEXT, frame)
        save.setObjectName(f"keys-entry-{key}-save")
        remove = QPushButton(REMOVE_TEXT, frame)
        remove.setObjectName(f"keys-entry-{key}-remove")
        row.addWidget(value, 1)
        row.addWidget(save)
        row.addWidget(remove)
        column.addLayout(row)
        column.addLayout(_action_row(mark))

        save.clicked.connect(lambda *_, n=name: self._on_save_value(n))
        value.returnPressed.connect(lambda *_, n=name: self._on_save_value(n))
        remove.clicked.connect(lambda *_, n=name: self._on_remove(n))
        return _Card(frame, status, value, mark)

    # ----------------------------------------------------------- state on screen

    def refresh(self) -> None:
        """Re-read which names are listed and whether each one is set. Values are never read into a control.

        This read runs on the GUI thread. It also supersedes a read still running, so that older read cannot bring back a
        name this read has already dropped.
        """
        self._load_generation += 1
        self._loading = False
        self._apply_entries(self._keys.entries())

    def _apply_entries(self, entries: list[Any]) -> None:
        listed = [entry for entry in entries if not entry.built_in]
        wanted = {entry.name for entry in listed}
        for name in [name for name in self._cards if name not in wanted]:
            card = self._cards.pop(name)
            self._entry_layout.removeWidget(card.frame)
            card.frame.hide()
            card.frame.deleteLater()
        for entry in listed:
            card = self._cards.get(entry.name)
            if card is None:
                card = self._make_card(entry.name)
                self._cards[entry.name] = card
                self._entry_layout.addWidget(card.frame)
            card.status.setText(STATUS_TEXT[entry.status])
            _show_mark(card.mark, entry.critical)
        for entry in entries:
            if entry.built_in and self._runpod_mark is not None:
                _show_mark(self._runpod_mark, entry.critical)
        self._reorder(entries)
        self._apply_filter()

    def _reorder(self, entries: list[Any]) -> None:
        """Lay the cards out in the order of ``entries``, which lists the critical names first. RunPod sits where its entry is."""
        frames: list[QFrame] = []
        for entry in entries:
            if entry.built_in:
                if self._runpod_frame is not None:
                    frames.append(self._runpod_frame)
            elif entry.name in self._cards:
                frames.append(self._cards[entry.name].frame)
        for index, frame in enumerate(frames):
            self._entry_layout.removeWidget(frame)
            self._entry_layout.insertWidget(index, frame)

    def _show(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    # ----------------------------------------------------------- search

    @Slot(str)
    def _on_search_changed(self, _text: str) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Show each card whose name holds the search text, ignoring case. Only names are matched, never values."""
        query = self._search.text().strip().casefold()
        frames = [(name, card.frame) for name, card in self._cards.items()]
        if self._runpod_frame is not None:
            frames.append((RUNPOD_NAME, self._runpod_frame))
        any_match = False
        for name, frame in frames:
            match = not query or query in name.casefold()
            frame.setVisible(match)
            any_match = any_match or match
        # Nothing is said while the names are still being read, because no card exists yet to match against.
        self._search_empty.setVisible(bool(query) and not any_match and not self._loading)

    # ----------------------------------------------------------- actions

    @Slot()
    def _on_add_clicked(self) -> None:
        try:
            listed = self._keys.save(self._new_name.text(), self._new_value.text())
        except (KeyNameError, ValueError) as exc:
            self._show(str(exc))  # fixed wording from api_keys; the value typed is not repeated
            return
        except SecretStoreError as exc:
            self._show(f"Not saved. {exc}")  # names the step and the class only
            return
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return
        self._new_name.clear()
        self._new_value.clear()
        self.refresh()
        self._show(ADDED_TEXT.format(name=listed))

    def _on_save_value(self, name: str) -> None:
        card = self._cards.get(name)
        if card is None:
            return
        typed = card.value.text()
        if not typed.strip():
            self._show(NO_VALUE_TEXT)
            return
        try:
            listed = self._keys.save(name, typed)
        except (KeyNameError, ValueError) as exc:
            self._show(str(exc))
            return
        except SecretStoreError as exc:
            self._show(f"Not saved. {exc}")
            return
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return
        card.value.clear()
        self.refresh()
        self._show(REPLACED_TEXT.format(name=listed))

    def _on_critical_toggled(self, name: str, critical: bool) -> None:
        try:
            listed = self._keys.set_critical(name, critical)
        except KeyNameError as exc:
            self.refresh()
            self._show(str(exc))
            return
        except OSError:
            # The box goes back to the mark the settings file still holds, because refresh reads it again.
            self.refresh()
            self._show(NOT_WRITTEN_TEXT)
            return
        self.refresh()
        self._show((CRITICAL_TEXT if critical else UNCRITICAL_TEXT).format(name=listed))

    def _on_user_env(self, name: str) -> None:
        if self._keys.status(name) == STATUS_NOT_SET:
            self._show(USER_ENV_NO_VALUE_TEXT.format(name=name))
            return
        try:
            variable = self._keys.environment_name(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        body = USER_ENV_BODY.format(name=name, variable=variable)
        ask = self._confirm if self._confirm is not None else _ask_user_env
        if not ask(self, USER_ENV_TITLE, body):
            self._show(USER_ENV_CANCELLED_TEXT)
            return
        try:
            written = self._keys.set_user_environment(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except (SecretStoreError, UserEnvironmentError) as exc:
            self._show(USER_ENV_NOT_SET_TEXT.format(detail=exc))  # names the step and the class only
            return
        except OSError:
            self._show(USER_ENV_OS_TEXT)
            return
        text = USER_ENV_SET_TEXT.format(variable=written.variable)
        if not written.announced:
            text = f"{text} {USER_ENV_UNANNOUNCED_TEXT}"
        self._show(text)

    def _on_remove(self, name: str) -> None:
        if name in BUILT_IN_NAMES:
            return  # RunPod keeps its own Remove key button, on its card
        body = REMOVE_BODY.format(name=name, listing=REMOVE_LISTING)
        ask = self._confirm if self._confirm is not None else _ask_remove
        if not ask(self, REMOVE_TITLE, body):
            self._show(CANCELLED_TEXT)
            return
        try:
            removed = self._keys.remove(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except SecretStoreError as exc:
            self._show(NOT_REMOVED_TEXT.format(detail=exc))
            return
        except OSError:
            # The keychain value is deleted before the names list is written, so it can already be gone here. The card is
            # read again, so it never says a value is set when the keychain no longer holds one.
            self.refresh()
            self._show(NOT_REMOVED_OS_TEXT)
            return
        self.refresh()
        self._show((REMOVED_TEXT if removed else REMOVED_NO_VALUE_TEXT).format(name=name))


__all__ = [
    "ADDED_TEXT",
    "CRITICAL_BADGE_TEXT",
    "CRITICAL_TEXT",
    "KeysSection",
    "NOT_WRITTEN_TEXT",
    "REMOVED_TEXT",
    "REPLACED_TEXT",
    "SEARCH_EMPTY_TEXT",
    "SEARCH_PLACEHOLDER",
    "STATUS_TEXT",
    "UNCRITICAL_TEXT",
    "USER_ENV_SET_TEXT",
    "USER_ENV_TITLE",
]
