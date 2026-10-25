"""A check that a text block shows markup-looking text as its own characters. Offscreen Qt, no network.

A text block is rich text, so its lines can be paragraphs. Each line is inserted as plain text (text.block_html), so a line
that looks like markup must show its characters, and nothing in it may be bold or italic.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextDocument
from PySide6.QtWidgets import QLabel

MARKUP_LINE = "<b>x</b> and <i>y</i>"


def assert_shows_literally(block: QLabel, text: str = MARKUP_LINE) -> None:
    """The block shows ``text`` as its own characters, case and all, and the characters are neither bold nor italic.

    The text is looked up in the laid-out document, so the check reads what the owner sees, not the block's source.
    """
    assert block.textFormat() == Qt.TextFormat.RichText, block.objectName()
    document = QTextDocument()
    document.setHtml(block.text())
    cursor = document.find(text, 0, QTextDocument.FindFlag.FindCaseSensitively)
    assert not cursor.isNull(), f"{text!r} is not shown as its own characters"
    char_format = cursor.charFormat()
    assert char_format.fontWeight() < QFont.Weight.Bold, f"{text!r} is shown bold"
    assert not char_format.fontItalic(), f"{text!r} is shown italic"
