"""A plain Yes or No question for the owner, and a plain notice. Qt code, so it sits in ui/.

No is the default button, and Escape means No. Only a click on Yes returns True, so an owner who presses Enter
without reading the question changes nothing. The text is shown as plain text, and the owner can select and copy it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from meridian_desktop.ui.text import make_box_selectable

YES_TEXT = "Yes"
NO_TEXT = "No"


def ask_yes_no(parent: QWidget, title: str, text: str) -> bool:
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
    make_box_selectable(box)
    box.exec()
    return box.clickedButton() is yes


def show_notice(parent: QWidget | None, title: str, text: str) -> None:
    """A modal notice with one OK button. Its text is plain, and the owner can select and copy it."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    make_box_selectable(box)
    box.exec()


__all__ = ["NO_TEXT", "YES_TEXT", "ask_yes_no", "show_notice"]
