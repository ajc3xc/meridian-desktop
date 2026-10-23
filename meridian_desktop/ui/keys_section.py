"""The Keys block on the Settings tab: the RunPod entry, one card per saved name, and a form that adds a name. Qt code.

The RunPod entry is the RunPod panel's own card (ui/runpod_panel.py), so its checks, its pod list and its key row work as
they did before. Every other name is a card of its own: its status, a password box for a new value, Save value and Remove.
A name is added with the form at the foot of the block.

Values go to the keychain through api_keys. This block never shows a value, never reads one back into a control, and never
writes one to the settings file. A status is read only to test whether a value is set. A failed read shows as unreadable.
Remove asks first, because the saved value is deleted from the keychain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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
YES_TEXT = "Yes"
NO_TEXT = "No"
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
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written."
RUNPOD_HEADING_TEXT = RUNPOD_NAME

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
    box.exec()
    return box.clickedButton() is yes


@dataclass
class _Card:
    """One saved name's card and the widgets the section reads and changes."""

    frame: QFrame
    status: QLabel
    value: QLineEdit


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
        self._build()
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(_label(self, "keys-intro", INTRO_TEXT))

        if self._runpod_card is not None:
            # The RunPod card is framed like the other cards, so every name in the block looks the same.
            frame = QFrame(self)
            frame.setObjectName("keys-entry-runpod")
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            frame_column = QVBoxLayout(frame)
            frame_column.addWidget(_bold(_label(frame, "keys-runpod-name", RUNPOD_HEADING_TEXT)))
            frame_column.addWidget(self._runpod_card)
            column.addWidget(frame)

        self._entry_layout = QVBoxLayout()
        self._entry_layout.setSpacing(8)
        column.addLayout(self._entry_layout)

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

    def _make_card(self, name: str) -> _Card:
        key = slug(name)
        frame = QFrame(self)
        frame.setObjectName(f"keys-entry-{key}")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(frame)

        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, f"keys-entry-{key}-name", name)))
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

        save.clicked.connect(lambda *_, n=name: self._on_save_value(n))
        value.returnPressed.connect(lambda *_, n=name: self._on_save_value(n))
        remove.clicked.connect(lambda *_, n=name: self._on_remove(n))
        return _Card(frame, status, value)

    # ----------------------------------------------------------- state on screen

    def refresh(self) -> None:
        """Re-read which names are listed and whether each one is set. Values are never read into a control."""
        listed = [entry for entry in self._keys.entries() if not entry.built_in]
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
        self.refresh()
        self._show((REMOVED_TEXT if removed else REMOVED_NO_VALUE_TEXT).format(name=name))


__all__ = [
    "ADDED_TEXT",
    "KeysSection",
    "NOT_WRITTEN_TEXT",
    "REMOVED_TEXT",
    "REPLACED_TEXT",
    "STATUS_TEXT",
]
