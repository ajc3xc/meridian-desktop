"""Writing area in Settings: DOCX and Slides, each shown as Not connected with its reason. Qt code, so it sits in ui/.

The area makes no request and reads nothing. The local Meridian server has no DOCX route and no Slides route yet,
so each line says so in plain words, and no document or deck is made up. The LaTeX engine check stays its own panel
(in Diagnostics under Settings), and the note in the area says where to find it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from meridian_desktop.ui.text import TextBlock

WRITING_SECTION = "Writing (DOCX and Slides on this computer)"
WRITING_INTRO = "The LaTeX engine check is in Diagnostics, under Settings."
DOCX_LINE = (
    "DOCX: Not connected. The local Meridian server has no DOCX route yet, "
    "so this desktop cannot read or write DOCX files."
)
SLIDES_LINE = (
    "Slides: Not connected. The local Meridian server has no Slides route yet, "
    "so this desktop cannot read slide decks."
)


class WritingArea(QWidget):
    """The DOCX and Slides lines, each Not connected. Nothing is read and nothing is sent."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("writing-area")
        layout = QVBoxLayout(self)
        # The intro and the two lines are one block, so one drag over them selects all three.
        lines = TextBlock(self, "writing-text", spacing=layout.spacing())
        lines.line("writing-intro", WRITING_INTRO)
        lines.line("writing-docx", DOCX_LINE)
        lines.line("writing-slides", SLIDES_LINE)
        layout.addWidget(lines.label)


__all__ = ["DOCX_LINE", "SLIDES_LINE", "WRITING_INTRO", "WRITING_SECTION", "WritingArea"]
