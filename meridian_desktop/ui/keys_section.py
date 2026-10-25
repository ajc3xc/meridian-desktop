"""The Keys block on the Settings tab: the RunPod entry, one card per saved name, and a form that adds a name. Qt code.

The RunPod entry is the RunPod panel's own card (ui/runpod_panel.py), so its checks, its pod list and its key row work as
they did before. Every other name is a card of its own: its status, a password box for a new value, Save value and Remove.
A name is added with the form at the foot of the block.

Values go to the keychain through api_keys. This block never shows a value, never reads one back into a control, and never
writes one to the settings file. A status is read only to test whether a value is set. A failed read shows as unreadable.
Remove asks first, because the saved value is deleted from the keychain.

A name that matches a known service (key_services) also shows what its key is for and where it is made. When that
service has a check, its card has a Check button. The check runs only when the owner presses it, on the thread pool,
and shows one fixed message. The form can pick a service, which fills in the name. Other leaves the name as typed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
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
    STATUS_NOT_SET,
    STATUS_SET,
    STATUS_UNREADABLE,
    ApiKeys,
    KeyNameError,
    slug,
)
from meridian_desktop.key_services import (
    NO_CHECK_TEXT,
    SERVICES,
    UNREACHABLE_TEXT,
    Transport,
    check_saved_key,
    service_for,
)
from meridian_desktop.secret_store import SecretStore, SecretStoreError
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import submit
from meridian_desktop.ui.text import make_selectable

INTRO_TEXT = (
    "Keys are kept in the OS keychain, one entry for each name. This list shows each name and whether a value is set. "
    "A value is never shown here. Type a new value to replace one."
)
ADD_HEADING_TEXT = "Add a key"
NAME_PLACEHOLDER = "Name, for example OpenAI"
VALUE_PLACEHOLDER = "Key value"
NEW_VALUE_PLACEHOLDER = "New value, to replace the saved one"
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
# The service list in the form. Its services come from the registry, and Other keeps the free name.
SERVICE_LABEL_TEXT = "Service"
OTHER_CHOICE = "Other"
SERVICE_CHOICES = (*(service.name for service in SERVICES), OTHER_CHOICE)
# A known service's card: what the key is for, where it is made, and the check.
CREATE_TEXT = "Create the key at {page}."
CHECK_TEXT = "Check"
CHECKING_TEXT = "Checking {service}..."

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


def _store_of(keys: ApiKeys) -> SecretStore:
    """The keychain store that ``keys`` reads its names' values from. A Check reads its one value through it.

    ApiKeys keeps the store private and returns no value, so this is the one place the block reads the attribute.
    The value a Check reads goes only to its request, and it is never kept or shown.
    """
    return keys._secrets  # noqa: SLF001 - the store the saved names are read from; ApiKeys itself returns no value


@dataclass
class _Card:
    """One saved name's card and the widgets the section reads and changes. A known service's card also has a Check."""

    frame: QFrame
    status: QLabel
    value: QLineEdit
    check: QPushButton | None = None
    check_result: QLabel | None = None


class KeysSection(QWidget):
    """The Keys block. The RunPod card is placed by the host; every other name gets a card made here."""

    def __init__(
        self,
        keys: ApiKeys,
        runpod_card: QWidget | None = None,
        parent: QWidget | None = None,
        *,
        confirm: Confirm | None = None,
        transport: Transport | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keys-section")
        self._keys = keys
        self._runpod_card = runpod_card
        # None means the Yes/No dialog. It is looked up when Remove is pressed, not here, so a test can swap it.
        self._confirm = confirm
        # The store a Check reads its value from, and the transport it sends with. None for the transport is urllib.
        self._store = _store_of(keys)
        self._transport = transport
        self._cards: dict[str, _Card] = {}
        # The first read of the listed names runs on the thread pool, so building this block never reads the keychain.
        self._owner = object()
        self._load_generation = 0
        self._loading = False
        # A Check also runs on the thread pool. Its result comes back under its own owner and token, so it is never
        # mistaken for a read of the names. The name maps to the token of the one check running for its card.
        self._check_owner = object()
        self._check_tokens = count(1)
        self._pending: dict[str, int] = {}
        shared_delivery().delivered.connect(self._on_names_loaded)
        shared_delivery().delivered.connect(self._on_check_delivered)
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

        if self._runpod_card is not None:
            # The RunPod card is framed like the other cards, so every name in the block looks the same. Its heading is
            # the first line of the card's own text block, so one drag over the heading and the status lines selects them.
            frame = QFrame(self)
            frame.setObjectName("keys-entry-runpod")
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            frame_column = QVBoxLayout(frame)
            frame_column.addWidget(self._runpod_card)
            column.addWidget(frame)

        self._entry_layout = QVBoxLayout()
        self._entry_layout.setSpacing(8)
        column.addLayout(self._entry_layout)

        self._message = _label(self, "keys-message")
        self._message.setVisible(False)
        column.addWidget(self._message)

        column.addWidget(_bold(_label(self, "keys-add-heading", ADD_HEADING_TEXT)))
        service_row = QHBoxLayout()
        self._new_service = QComboBox(self)
        self._new_service.setObjectName("keys-new-service")
        self._new_service.setAccessibleName(SERVICE_LABEL_TEXT)
        self._new_service.addItems(list(SERVICE_CHOICES))
        self._new_service.setCurrentText(OTHER_CHOICE)
        # activated fires only for a pick the owner makes, so the name is never filled by a reset.
        self._new_service.activated.connect(self._on_service_picked)
        service_row.addWidget(_label(self, "keys-service-label", SERVICE_LABEL_TEXT))
        service_row.addWidget(self._new_service)
        service_row.addStretch(1)
        column.addLayout(service_row)
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

    def _make_card(self, name: str) -> _Card:
        key = slug(name)
        service = service_for(name)
        frame = QFrame(self)
        frame.setObjectName(f"keys-entry-{key}")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(frame)

        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, f"keys-entry-{key}-name", name)))
        status = _label(frame, f"keys-entry-{key}-status")
        head.addWidget(status, 1)
        column.addLayout(head)

        if service is not None:
            # A known service says what its key is for and where it is made. Both lines are plain text.
            column.addWidget(_label(frame, f"keys-entry-{key}-description", service.description))
            column.addWidget(_label(frame, f"keys-entry-{key}-create", CREATE_TEXT.format(page=service.create_page)))

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

        check: QPushButton | None = None
        check_result: QLabel | None = None
        if service is not None:
            check_row = QHBoxLayout()
            check_result = _label(frame, f"keys-entry-{key}-check-result")
            if service.check is None:
                # A service with no check says so in place of a button, so nothing looks as if it were checked.
                check_result.setText(NO_CHECK_TEXT)
            else:
                check = QPushButton(CHECK_TEXT, frame)
                check.setObjectName(f"keys-entry-{key}-check")
                check.clicked.connect(lambda *_, n=name: self._on_check(n))
                check_row.addWidget(check)
            check_row.addWidget(check_result, 1)
            column.addLayout(check_row)

        save.clicked.connect(lambda *_, n=name: self._on_save_value(n))
        value.returnPressed.connect(lambda *_, n=name: self._on_save_value(n))
        remove.clicked.connect(lambda *_, n=name: self._on_remove(n))
        return _Card(frame, status, value, check, check_result)

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
            self._pending.pop(name, None)  # a check still running for a dropped card has no card to show its result on
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

    def _show(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

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
        self._new_service.setCurrentText(OTHER_CHOICE)
        self._reset_check(listed)
        self.refresh()
        self._show(ADDED_TEXT.format(name=listed))

    @Slot(int)
    def _on_service_picked(self, index: int) -> None:
        """Fill the name with the service the owner picked. Other leaves the name as the owner typed it."""
        choice = self._new_service.itemText(index)
        if choice != OTHER_CHOICE:
            self._new_name.setText(choice)

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
        self._reset_check(listed)
        self.refresh()
        self._show(REPLACED_TEXT.format(name=listed))

    def _reset_check(self, name: str) -> None:
        """A saved value changed, so an older check for the name is dropped and its result cleared.

        A check still running is dropped here too, so its result, when it arrives, has no pending token to match.
        """
        self._pending.pop(name, None)
        card = self._cards.get(name)
        if card is not None and card.check is not None and card.check_result is not None:
            card.check.setEnabled(True)
            card.check_result.setText("")

    def _on_check(self, name: str) -> None:
        """Start the owner's check of the saved key for ``name``. It runs on the thread pool and sends one GET."""
        card = self._cards.get(name)
        service = service_for(name)
        if card is None or card.check is None or card.check_result is None or service is None:
            return
        if name in self._pending:
            return  # one check at a time for a card
        token = next(self._check_tokens)
        self._pending[name] = token
        card.check.setEnabled(False)
        card.check_result.setText(CHECKING_TEXT.format(service=service.name))
        # The job holds the service, the name, the store and the transport, never this block, so no widget is touched.
        store, transport = self._store, self._transport
        submit(self._check_owner, token, lambda: check_saved_key(service, name, store, transport=transport))

    @Slot(object)
    def _on_check_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another section, for a check that was reset, or for a gone card is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._check_owner:
            return
        token, (message, failure) = item.value
        name = next((listed for listed, pending in self._pending.items() if pending == token), None)
        if name is None:
            return
        del self._pending[name]
        card = self._cards.get(name)
        if card is None or card.check is None or card.check_result is None:
            return
        card.check.setEnabled(True)
        if failure is None and isinstance(message, str):
            card.check_result.setText(message)
            return
        # The worker raised, so there is no message from it. Show the unreachable text, which names no value.
        service = service_for(name)
        card.check_result.setText(UNREACHABLE_TEXT.format(service=service.name if service is not None else name))

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
    "CHECKING_TEXT",
    "CHECK_TEXT",
    "CREATE_TEXT",
    "KeysSection",
    "NOT_WRITTEN_TEXT",
    "OTHER_CHOICE",
    "REMOVED_TEXT",
    "REPLACED_TEXT",
    "SERVICE_CHOICES",
    "STATUS_TEXT",
]
