"""The Limits block on the Settings tab: one slider per owner limit, with its current value beside it. Qt code, in ui/.

Each limit is a whole number kept in the settings file (settings_store.LIMIT_SPECS). The block shows the saved value on every
refresh. A slider the owner moves shows its value as it moves, and saves when the owner lets go of it, or on each step from
the keyboard. The store clamps every value to its range. Enforcement happens on the server and is not active yet, so nothing
here stops an upload, a write, a claim or a download.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QSlider, QVBoxLayout, QWidget

from meridian_desktop.settings_store import LIMIT_SPECS, SettingsStore, SettingsUnreadable
from meridian_desktop.ui.text import make_selectable

LIMITS_TITLE = "Limits"
LIMITS_INTRO = (
    "Downloads of any size are never asked about or blocked, and virus scanning always applies. The total-size note only "
    "shows a message. The other limits are saved in the settings file. Enforcement happens on the server, and it is not "
    "active yet, so these values do not stop an upload, a write, a claim or a download."
)
APPROVAL_ENABLED_TEXT = "Show the total-file-size note"
NOT_SAVED_TEXT = "Not saved: the settings file could not be written ({name})."


@dataclass(frozen=True)
class LimitRow:
    """One slider on the Limits block: the settings key it saves to, its caption, and the unit shown beside its value."""

    key: str
    caption: str
    unit: str


LIMIT_ROWS = (
    LimitRow("limit_approval_total_mb", "Total file size note at", "MB"),
    LimitRow("limit_chat_total_mb", "Per-chat cap (MB in one chat)", "MB"),
    LimitRow("limit_file_mb", "Per-file size cap", "MB"),
    LimitRow("limit_zip_download_mb", "Estimated zip download cap", "MB"),
    LimitRow("limit_writes", "Writing limit (writes per session)", "writes"),
    LimitRow("limit_claims", "Claims limit (file claims per session)", "claims"),
)


def value_text(value: int, unit: str) -> str:
    """The saved value as the Limits block shows it beside its slider."""
    return f"{value} {unit}"


class LimitsSection(QWidget):
    """The Limits block. Build it once; call refresh() to show the saved values again."""

    def __init__(self, store: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("limits-section")
        self._store = store
        self._specs = {spec.key: spec for spec in LIMIT_SPECS}
        self._units = {row.key: row.unit for row in LIMIT_ROWS}
        self._sliders: dict[str, QSlider] = {}
        self._values: dict[str, QLabel] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        intro = make_selectable(QLabel(LIMITS_INTRO, self))
        intro.setObjectName("limits-intro")
        intro.setWordWrap(True)
        column.addWidget(intro)

        self._approval_enabled = QCheckBox(APPROVAL_ENABLED_TEXT, self)
        self._approval_enabled.setObjectName("limits-approval-enabled")
        self._approval_enabled.toggled.connect(self._on_approval_enabled_toggled)
        column.addWidget(self._approval_enabled)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        for index, row in enumerate(LIMIT_ROWS):
            self._add_row(grid, index, row)
        column.addLayout(grid)

        self._status = make_selectable(QLabel(self))
        self._status.setObjectName("limits-status")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        column.addWidget(self._status)
        self.refresh()

    # ----------------------------------------------------------- layout

    def _add_row(self, grid: QGridLayout, index: int, row: LimitRow) -> None:
        spec = self._specs[row.key]

        caption = make_selectable(QLabel(row.caption, self))
        caption.setObjectName(f"limit-caption-{row.key}")
        caption.setWordWrap(True)
        grid.addWidget(caption, index, 0)

        slider = QSlider(Qt.Orientation.Horizontal, self)
        slider.setObjectName(f"limit-slider-{row.key}")
        slider.setAccessibleName(row.caption)
        slider.setRange(spec.minimum, spec.maximum)
        # Without tracking, a drag saves once on release. The keyboard and the wheel still save on each step.
        slider.setTracking(False)
        slider.setMinimumWidth(140)
        slider.sliderMoved.connect(lambda position, key=row.key: self._show_value(key, position))
        slider.valueChanged.connect(lambda value, key=row.key: self._on_value_changed(key, value))
        self._sliders[row.key] = slider
        grid.addWidget(slider, index, 1)

        value = make_selectable(QLabel(self))
        value.setObjectName(f"limit-value-{row.key}")
        value.setMinimumWidth(90)
        self._values[row.key] = value
        grid.addWidget(value, index, 2)

        allowed = make_selectable(QLabel(f"{spec.minimum} to {spec.maximum} {row.unit}", self))
        allowed.setObjectName(f"limit-range-{row.key}")
        grid.addWidget(allowed, index, 3)

    # ----------------------------------------------------------- showing the saved values

    def refresh(self) -> None:
        """Show the saved values. Showing them never saves anything."""
        settings = self._store.load().settings
        self._approval_enabled.blockSignals(True)  # showing the saved switch must not save it again
        self._approval_enabled.setChecked(settings.limit_approval_enabled)
        self._approval_enabled.blockSignals(False)
        for row in LIMIT_ROWS:
            value = getattr(settings, row.key)
            slider = self._sliders[row.key]
            slider.blockSignals(True)  # showing the saved value must not save it again
            slider.setValue(value)
            slider.blockSignals(False)
            self._show_value(row.key, value)

    def _show_value(self, key: str, value: int) -> None:
        self._values[key].setText(value_text(value, self._units[key]))

    # ----------------------------------------------------------- saving

    def _on_value_changed(self, key: str, value: int) -> None:
        """Save the owner's value through the store, which clamps it. A value that is not saved is shown as the file has it."""
        self._show_value(key, value)
        try:
            self._store.set_limit(key, value)
        except SettingsUnreadable as exc:
            # The text is the owner's message: a copy of the unreadable file is kept, and nothing is written.
            self._show_status(str(exc))
        except OSError as exc:
            self._show_status(NOT_SAVED_TEXT.format(name=exc.__class__.__name__))
        else:
            self._show_status("")
        self.refresh()

    def _on_approval_enabled_toggled(self, checked: bool) -> None:
        """Save the total-size note switch through the store. A download is never blocked or asked about, in either state."""
        try:
            self._store.set_limit_approval_enabled(bool(checked))
        except SettingsUnreadable as exc:
            self._show_status(str(exc))
        except OSError as exc:
            self._show_status(NOT_SAVED_TEXT.format(name=exc.__class__.__name__))
        else:
            self._show_status("")
        self.refresh()

    def _show_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))


__all__ = ["LIMITS_INTRO", "LIMITS_TITLE", "LIMIT_ROWS", "LimitRow", "LimitsSection", "value_text"]
